/* CPython extension: thin, fast binding over the c0 single-header C core.
 *
 * The scan-heavy work happens in C; the reader functions return byte offsets
 * so the Python layer can hand back zero-copy memoryview slices over the input
 * buffer. */
#define PY_SSIZE_T_CLEAN
#include <Python.h>

#define C0_IMPLEMENTATION
#include "c0.h"

static PyObject *py_is_assigned(PyObject *self, PyObject *args) {
    int b;
    (void)self;
    if (!PyArg_ParseTuple(args, "i", &b)) return NULL;
    return PyBool_FromLong(c0_is_assigned((uint8_t)b));
}

static PyObject *py_canonical(PyObject *self, PyObject *args) {
    Py_buffer v;
    int r;
    (void)self;
    if (!PyArg_ParseTuple(args, "y*", &v)) return NULL;
    r = c0_canonical((const uint8_t *)v.buf, (size_t)v.len);
    PyBuffer_Release(&v);
    return PyBool_FromLong(r);
}

static PyObject *py_unescape(PyObject *self, PyObject *args) {
    Py_buffer v;
    PyObject *out;
    size_t n;
    (void)self;
    if (!PyArg_ParseTuple(args, "y*", &v)) return NULL;
    out = PyBytes_FromStringAndSize(NULL, v.len);
    if (!out) {
        PyBuffer_Release(&v);
        return NULL;
    }
    n = c0_unescape((const uint8_t *)v.buf, (size_t)v.len,
                    (uint8_t *)PyBytes_AS_STRING(out));
    PyBuffer_Release(&v);
    if (_PyBytes_Resize(&out, (Py_ssize_t)n) != 0) return NULL;
    return out;
}

static PyObject *py_tokenize(PyObject *self, PyObject *args) {
    Py_buffer v;
    c0_tokenizer tz;
    c0_token t;
    c0_step s;
    PyObject *out;
    (void)self;
    if (!PyArg_ParseTuple(args, "y*", &v)) return NULL;
    out = PyList_New(0);
    if (!out) {
        PyBuffer_Release(&v);
        return NULL;
    }
    c0_tokenizer_init(&tz, (const uint8_t *)v.buf, (size_t)v.len);
    while ((s = c0_tokenizer_next(&tz, &t)) == C0_TOKEN) {
        PyObject *tup = Py_BuildValue("(inn)", (int)t.type,
                                      (Py_ssize_t)t.start, (Py_ssize_t)t.end);
        if (!tup || PyList_Append(out, tup) != 0) {
            Py_XDECREF(tup);
            Py_DECREF(out);
            PyBuffer_Release(&v);
            return NULL;
        }
        Py_DECREF(tup);
    }
    PyBuffer_Release(&v);
    if (s == C0_ERROR) {
        Py_DECREF(out);
        PyErr_SetString(PyExc_ValueError,
                        tz.error == C0_ERR_UNASSIGNED
                            ? "unassigned control code"
                            : "unexpected end of input after DLE escape");
        return NULL;
    }
    return out;
}

/* Append a (start, end) tuple for the slice [b.ptr, b.ptr+b.len) into list. */
static int append_span(PyObject *list, const uint8_t *base, c0_bytes b) {
    Py_ssize_t s = (Py_ssize_t)(b.ptr - base);
    PyObject *tup = Py_BuildValue("(nn)", s, s + (Py_ssize_t)b.len);
    int rc;
    if (!tup) return -1;
    rc = PyList_Append(list, tup);
    Py_DECREF(tup);
    return rc;
}

static PyObject *py_table(PyObject *self, PyObject *args) {
    Py_buffer v;
    Py_ssize_t offset = 0;
    const uint8_t *base;
    c0_group g;
    c0_bytes nm, h, rec;
    c0_iter hi, ri;
    PyObject *headers, *records, *result;
    Py_ssize_t ns;
    (void)self;
    if (!PyArg_ParseTuple(args, "y*|n", &v, &offset)) return NULL;
    base = (const uint8_t *)v.buf;
    g.buf = base;
    g.start = (size_t)offset;
    g.end = (size_t)v.len;

    headers = PyList_New(0);
    records = PyList_New(0);
    if (!headers || !records) goto fail;

    hi = c0_group_headers(g);
    while (c0_next_header(&hi, &h)) {
        if (append_span(headers, base, h) != 0) goto fail;
    }
    ri = c0_group_records(g);
    while (c0_next_record(&ri, &rec)) {
        if (append_span(records, base, rec) != 0) goto fail;
    }

    nm = c0_group_name(g);
    ns = (Py_ssize_t)(nm.ptr - base);
    result = Py_BuildValue("(nnNN)", ns, ns + (Py_ssize_t)nm.len, headers, records);
    PyBuffer_Release(&v);
    return result;

fail:
    Py_XDECREF(headers);
    Py_XDECREF(records);
    PyBuffer_Release(&v);
    return NULL;
}

static PyObject *py_record_fields(PyObject *self, PyObject *args) {
    Py_buffer v;
    Py_ssize_t start, end;
    const uint8_t *base;
    c0_bytes rec, f;
    c0_field_iter fi;
    PyObject *out;
    (void)self;
    if (!PyArg_ParseTuple(args, "y*nn", &v, &start, &end)) return NULL;
    base = (const uint8_t *)v.buf;
    rec.ptr = base + start;
    rec.len = (size_t)(end - start);
    out = PyList_New(0);
    if (!out) {
        PyBuffer_Release(&v);
        return NULL;
    }
    fi = c0_record_fields(rec);
    while (c0_next_field(&fi, &f)) {
        if (append_span(out, base, f) != 0) {
            Py_DECREF(out);
            PyBuffer_Release(&v);
            return NULL;
        }
    }
    PyBuffer_Release(&v);
    return out;
}

static PyObject *py_document(PyObject *self, PyObject *args) {
    Py_buffer v;
    const uint8_t *base;
    size_t len;
    c0_bytes nm;
    c0_doc_iter di;
    c0_group g;
    PyObject *groups, *result;
    Py_ssize_t ns;
    (void)self;
    if (!PyArg_ParseTuple(args, "y*", &v)) return NULL;
    base = (const uint8_t *)v.buf;
    len = (size_t)v.len;
    groups = PyList_New(0);
    if (!groups) {
        PyBuffer_Release(&v);
        return NULL;
    }
    di = c0_doc(base, len);
    while (c0_next_group(&di, &g)) {
        PyObject *tup = Py_BuildValue("(nn)", (Py_ssize_t)g.start, (Py_ssize_t)g.end);
        if (!tup || PyList_Append(groups, tup) != 0) {
            Py_XDECREF(tup);
            Py_DECREF(groups);
            PyBuffer_Release(&v);
            return NULL;
        }
        Py_DECREF(tup);
    }
    nm = c0_doc_name(base, len);
    ns = (Py_ssize_t)(nm.ptr - base);
    result = Py_BuildValue("(nnN)", ns, ns + (Py_ssize_t)nm.len, groups);
    PyBuffer_Release(&v);
    return result;
}

static PyObject *py_stream(PyObject *self, PyObject *args) {
    Py_buffer v;
    const uint8_t *base;
    c0_stream s;
    c0_block_iter bi;
    c0_bytes blk;
    PyObject *blocks, *torn, *result;
    (void)self;
    if (!PyArg_ParseTuple(args, "y*", &v)) return NULL;
    base = (const uint8_t *)v.buf;
    s = c0_stream_read(base, (size_t)v.len);
    blocks = PyList_New(0);
    if (!blocks) {
        PyBuffer_Release(&v);
        return NULL;
    }
    bi = c0_stream_blocks(&s);
    while (c0_next_block(&bi, &blk)) {
        if (append_span(blocks, base, blk) != 0) {
            Py_DECREF(blocks);
            PyBuffer_Release(&v);
            return NULL;
        }
    }
    torn = PyBool_FromLong(s.torn);
    result = Py_BuildValue("(nNN)", (Py_ssize_t)s.committed_end, torn, blocks);
    PyBuffer_Release(&v);
    return result;
}

static PyObject *py_pretty_format(PyObject *self, PyObject *args) {
    Py_buffer v;
    const char *indent = NULL;
    char *p;
    size_t outlen;
    PyObject *out;
    (void)self;
    if (!PyArg_ParseTuple(args, "y*|z", &v, &indent)) return NULL;
    p = c0_pretty_format((const uint8_t *)v.buf, (size_t)v.len, indent, &outlen);
    PyBuffer_Release(&v);
    if (!p) return PyErr_NoMemory();
    out = PyUnicode_FromStringAndSize(p, (Py_ssize_t)outlen);
    free(p);
    return out;
}

static PyObject *py_pretty_parse(PyObject *self, PyObject *args) {
    const char *s;
    Py_ssize_t slen;
    uint8_t *b;
    size_t outlen;
    PyObject *out;
    (void)self;
    if (!PyArg_ParseTuple(args, "s#", &s, &slen)) return NULL;
    b = c0_pretty_parse(s, (size_t)slen, &outlen);
    if (!b) return PyErr_NoMemory();
    out = PyBytes_FromStringAndSize((const char *)b, (Py_ssize_t)outlen);
    free(b);
    return out;
}

static PyMethodDef c0_methods[] = {
    {"is_assigned", py_is_assigned, METH_VARARGS, "Whether a byte is an assigned C0 control code."},
    {"canonical", py_canonical, METH_VARARGS, "Whether bytes are a canonical document unit."},
    {"unescape", py_unescape, METH_VARARGS, "Decode DLE escapes, returning the logical bytes."},
    {"tokenize", py_tokenize, METH_VARARGS, "List of (type, start, end) tokens; raises ValueError on bad input."},
    {"table", py_table, METH_VARARGS, "(name_start, name_end, [header spans], [record spans])."},
    {"record_fields", py_record_fields, METH_VARARGS, "Field spans within a record [start, end)."},
    {"document", py_document, METH_VARARGS, "(name_start, name_end, [group spans])."},
    {"stream", py_stream, METH_VARARGS, "(committed_end, torn, [block spans])."},
    {"pretty_format", py_pretty_format, METH_VARARGS, "Format compact bytes as a pretty string."},
    {"pretty_parse", py_pretty_parse, METH_VARARGS, "Parse a pretty string back to compact bytes."},
    {NULL, NULL, 0, NULL}};

static struct PyModuleDef c0_module = {
    PyModuleDef_HEAD_INIT, "_c0", "C0DATA C core binding", -1, c0_methods};

PyMODINIT_FUNC PyInit__c0(void) {
    return PyModule_Create(&c0_module);
}
