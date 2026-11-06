/* CPython extension: thin binding over the c0 single-header C core (1.0).
 *
 * The scan-heavy work happens in C; the reader functions return byte offsets
 * (start, end) into the caller's buffer so the Python layer can hand back
 * zero-copy memoryview slices. Errors are raised as the classes defined in
 * c0.errors, which this module imports when it loads. The one owned
 * resource, the builder, is a Python type whose deallocator frees it. */
#define PY_SSIZE_T_CLEAN
#include <Python.h>

#define C0_IMPLEMENTATION
#include "c0.h"

/* ---- errors --------------------------------------------------------------- */

static PyObject *Exc_C0Error, *Exc_Syntax, *Exc_Unassigned, *Exc_UnexpectedEnd,
    *Exc_UnitType, *Exc_NotFound, *Exc_Build;

static const char *status_message(c0_status code) {
    switch (code) {
    case C0_OK: return "ok";
    case C0_ERR_SYNTAX: return "malformed input";
    case C0_ERR_UNASSIGNED: return "unassigned control code";
    case C0_ERR_UNEXPECTED_END: return "unexpected end of input";
    case C0_ERR_TOO_DEEP: return "nesting too deep";
    case C0_ERR_NOT_SCALAR: return "unit is a level or a reference, not a value";
    case C0_ERR_NOT_LEVEL: return "unit is not a nested level";
    case C0_ERR_NOT_LIST: return "level is not a list";
    case C0_ERR_NOT_REFERENCE: return "unit is not a reference";
    case C0_ERR_NEEDS_DECODE: return "text needs decoding";
    case C0_ERR_NOT_FOUND: return "not found";
    case C0_ERR_BAD_NAME: return "not a name: control byte, edge whitespace or empty where a name is required";
    case C0_ERR_BAD_STATE: return "builder in a bad state: a level closed that was never opened, or an empty note";
    case C0_ERR_OOM: return "out of memory";
    }
    return "error";
}

/* Set a Python exception for a c0 status. `err` may be NULL. Always returns NULL. */
static PyObject *raise_status(c0_status code, const c0_error *err) {
    const char *msg = (err && err->msg && err->msg[0]) ? err->msg : status_message(code);
    size_t pos = err ? err->pos : 0;
    PyObject *exc;
    switch (code) {
    case C0_ERR_OOM:
        return PyErr_NoMemory();
    case C0_ERR_SYNTAX:
    case C0_ERR_TOO_DEEP:
    case C0_ERR_NEEDS_DECODE:
        exc = Exc_Syntax;
        break;
    case C0_ERR_UNASSIGNED:
        exc = Exc_Unassigned;
        break;
    case C0_ERR_UNEXPECTED_END:
        exc = Exc_UnexpectedEnd;
        break;
    case C0_ERR_NOT_SCALAR:
    case C0_ERR_NOT_LEVEL:
    case C0_ERR_NOT_LIST:
    case C0_ERR_NOT_REFERENCE:
        exc = Exc_UnitType;
        break;
    case C0_ERR_NOT_FOUND:
        exc = Exc_NotFound;
        break;
    case C0_ERR_BAD_NAME:
    case C0_ERR_BAD_STATE:
        exc = Exc_Build;
        break;
    default:
        exc = Exc_C0Error;
    }
    if (exc == Exc_Syntax || exc == Exc_Unassigned || exc == Exc_UnexpectedEnd) {
        PyObject *inst = PyObject_CallFunction(exc, "sn", msg, (Py_ssize_t)pos);
        if (!inst) return NULL;
        PyErr_SetObject(exc, inst);
        Py_DECREF(inst);
    } else {
        PyErr_SetString(exc, msg);
    }
    return NULL;
}

/* ---- helpers -------------------------------------------------------------- */

/* A (start, end) tuple for the slice `b` of a buffer starting at `base`. */
static PyObject *span(const uint8_t *base, c0_bytes b) {
    Py_ssize_t s = (Py_ssize_t)(b.ptr - base);
    return Py_BuildValue("(nn)", s, s + (Py_ssize_t)b.len);
}

static int append_span(PyObject *list, const uint8_t *base, c0_bytes b) {
    PyObject *t = span(base, b);
    int rc;
    if (!t) return -1;
    rc = PyList_Append(list, t);
    Py_DECREF(t);
    return rc;
}

static int append_steal(PyObject *list, PyObject *item) {
    int rc;
    if (!item) return -1;
    rc = PyList_Append(list, item);
    Py_DECREF(item);
    return rc;
}

/* Parse "y*nn": a buffer and a [start, end) range inside it. */
static int parse_range(PyObject *args, Py_buffer *v, Py_ssize_t *s, Py_ssize_t *e) {
    if (!PyArg_ParseTuple(args, "y*nn", v, s, e)) return 0;
    if (*s < 0 || *e < *s || *e > v->len) {
        PyBuffer_Release(v);
        PyErr_SetString(PyExc_IndexError, "range outside the buffer");
        return 0;
    }
    return 1;
}

static c0_bytes slice_of(const Py_buffer *v, Py_ssize_t s, Py_ssize_t e) {
    c0_bytes b;
    b.ptr = (const uint8_t *)v->buf + s;
    b.len = (size_t)(e - s);
    return b;
}

static c0_body body_of(const Py_buffer *v, Py_ssize_t s, Py_ssize_t e) {
    c0_body b;
    b.buf = (const uint8_t *)v->buf;
    b.start = (size_t)s;
    b.stop = (size_t)e;
    return b;
}

static c0_record record_of(const Py_buffer *v, Py_ssize_t s, Py_ssize_t e) {
    c0_record r;
    r.buf = (const uint8_t *)v->buf;
    r.start = (size_t)s;
    r.stop = (size_t)e;
    return r;
}

/* A group tuple: (depth, name_start, name_end, start, stop). */
static PyObject *group_tuple(const c0_group *g) {
    return Py_BuildValue("(nnnnn)", (Py_ssize_t)g->depth, (Py_ssize_t)g->name_start,
                         (Py_ssize_t)g->name_end, (Py_ssize_t)g->start, (Py_ssize_t)g->stop);
}

/* Parse the group tuple from args after the buffer: "y*nnnnn". */
static int parse_group(PyObject *args, Py_buffer *v, c0_group *g) {
    Py_ssize_t depth, ns, ne, s, e;
    if (!PyArg_ParseTuple(args, "y*nnnnn", v, &depth, &ns, &ne, &s, &e)) return 0;
    if (ns < 0 || ne < ns || s < 0 || e < s || e > v->len || ne > v->len) {
        PyBuffer_Release(v);
        PyErr_SetString(PyExc_IndexError, "range outside the buffer");
        return 0;
    }
    g->buf = (const uint8_t *)v->buf;
    g->depth = (size_t)depth;
    g->name_start = (size_t)ns;
    g->name_end = (size_t)ne;
    g->start = (size_t)s;
    g->stop = (size_t)e;
    return 1;
}

/* ---- bytewise helpers ----------------------------------------------------- */

static PyObject *py_is_assigned(PyObject *self, PyObject *args) {
    int b;
    (void)self;
    if (!PyArg_ParseTuple(args, "i", &b)) return NULL;
    return PyBool_FromLong(b >= 0 && b < 256 && c0_is_assigned((uint8_t)b));
}

static PyObject *py_is_layout(PyObject *self, PyObject *args) {
    int b;
    (void)self;
    if (!PyArg_ParseTuple(args, "i", &b)) return NULL;
    return PyBool_FromLong(b >= 0 && b < 256 && c0_is_layout((uint8_t)b));
}

static PyObject *py_is_text_escaped(PyObject *self, PyObject *args) {
    int b;
    (void)self;
    if (!PyArg_ParseTuple(args, "i", &b)) return NULL;
    return PyBool_FromLong(b >= 0 && b < 256 && c0_is_text_escaped((uint8_t)b));
}

static PyObject *py_is_plain(PyObject *self, PyObject *args) {
    Py_buffer v;
    int r;
    (void)self;
    if (!PyArg_ParseTuple(args, "y*", &v)) return NULL;
    r = c0_is_plain((const uint8_t *)v.buf, (size_t)v.len);
    PyBuffer_Release(&v);
    return PyBool_FromLong(r);
}

static PyObject *py_is_name(PyObject *self, PyObject *args) {
    Py_buffer v;
    int r;
    (void)self;
    if (!PyArg_ParseTuple(args, "y*", &v)) return NULL;
    r = c0_is_name((const uint8_t *)v.buf, (size_t)v.len);
    PyBuffer_Release(&v);
    return PyBool_FromLong(r);
}

static PyObject *py_decode_text(PyObject *self, PyObject *args) {
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
    n = c0_decode_text((const uint8_t *)v.buf, (size_t)v.len, (uint8_t *)PyBytes_AS_STRING(out));
    PyBuffer_Release(&v);
    if (_PyBytes_Resize(&out, (Py_ssize_t)n) != 0) return NULL;
    return out;
}

/* ---- validation ----------------------------------------------------------- */

static PyObject *py_validate(PyObject *self, PyObject *args) {
    Py_buffer v;
    c0_error err;
    c0_status st;
    (void)self;
    if (!PyArg_ParseTuple(args, "y*", &v)) return NULL;
    st = c0_validate((const uint8_t *)v.buf, (size_t)v.len, &err);
    PyBuffer_Release(&v);
    if (st != C0_OK) return raise_status(st, &err);
    Py_RETURN_NONE;
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

/* ---- units and values ----------------------------------------------------- */

static PyObject *py_unit_type(PyObject *self, PyObject *args) {
    Py_buffer v;
    Py_ssize_t s, e;
    int t;
    (void)self;
    if (!parse_range(args, &v, &s, &e)) return NULL;
    t = (int)c0_unit_type(slice_of(&v, s, e));
    PyBuffer_Release(&v);
    return PyLong_FromLong(t);
}

/* The logical value of the raw unit [s, e) as bytes. */
static PyObject *py_value(PyObject *self, PyObject *args) {
    Py_buffer v;
    Py_ssize_t s, e;
    c0_bytes raw, out;
    uint8_t *scratch;
    c0_status st;
    PyObject *res;
    (void)self;
    if (!parse_range(args, &v, &s, &e)) return NULL;
    raw = slice_of(&v, s, e);
    scratch = (uint8_t *)PyMem_Malloc(raw.len ? raw.len : 1);
    if (!scratch) {
        PyBuffer_Release(&v);
        return PyErr_NoMemory();
    }
    st = c0_value(raw, scratch, &out);
    if (st != C0_OK) {
        PyMem_Free(scratch);
        PyBuffer_Release(&v);
        return raise_status(st, NULL);
    }
    res = PyBytes_FromStringAndSize((const char *)out.ptr, (Py_ssize_t)out.len);
    PyMem_Free(scratch);
    PyBuffer_Release(&v);
    return res;
}

/* The raw unit [s, e) as a level: the (start, stop) of its body. */
static PyObject *py_unit_level(PyObject *self, PyObject *args) {
    Py_buffer v;
    Py_ssize_t s, e, off;
    c0_body body;
    c0_status st;
    (void)self;
    if (!parse_range(args, &v, &s, &e)) return NULL;
    st = c0_unit_level(slice_of(&v, s, e), &body);
    if (st != C0_OK) {
        PyBuffer_Release(&v);
        return raise_status(st, NULL);
    }
    /* the body's buf is the trimmed unit: rebase its range onto the buffer */
    off = (Py_ssize_t)(body.buf - (const uint8_t *)v.buf);
    PyBuffer_Release(&v);
    return Py_BuildValue("(nn)", off + (Py_ssize_t)body.start, off + (Py_ssize_t)body.stop);
}

/* The raw unit [s, e) as a reference: a list of segment spans. */
static PyObject *py_unit_ref(PyObject *self, PyObject *args) {
    Py_buffer v;
    Py_ssize_t s, e;
    c0_ref ref;
    c0_status st;
    PyObject *out;
    size_t i;
    (void)self;
    if (!parse_range(args, &v, &s, &e)) return NULL;
    st = c0_unit_ref(slice_of(&v, s, e), &ref);
    if (st != C0_OK) {
        PyBuffer_Release(&v);
        return raise_status(st, NULL);
    }
    out = PyList_New(0);
    if (out) {
        for (i = 0; i < ref.count; i++) {
            if (append_span(out, (const uint8_t *)v.buf, ref.seg[i]) != 0) {
                Py_CLEAR(out);
                break;
            }
        }
    }
    PyBuffer_Release(&v);
    return out;
}

/* Drain a unit iterator into a list of spans; raises on its error. */
static PyObject *drain_units(c0_unit_iter *it, const uint8_t *base) {
    PyObject *out = PyList_New(0);
    c0_bytes raw;
    if (!out) return NULL;
    while (c0_next_unit(it, &raw)) {
        if (append_span(out, base, raw) != 0) {
            Py_DECREF(out);
            return NULL;
        }
    }
    if (it->err.code) {
        Py_DECREF(out);
        return raise_status(it->err.code, &it->err);
    }
    return out;
}

/* The raw unit [s, e) as a flat list: item spans. */
static PyObject *py_unit_list(PyObject *self, PyObject *args) {
    Py_buffer v;
    Py_ssize_t s, e;
    c0_unit_iter it;
    c0_status st;
    PyObject *out;
    (void)self;
    if (!parse_range(args, &v, &s, &e)) return NULL;
    st = c0_unit_list(slice_of(&v, s, e), &it);
    if (st != C0_OK) {
        PyBuffer_Release(&v);
        return raise_status(st, NULL);
    }
    out = drain_units(&it, (const uint8_t *)v.buf);
    PyBuffer_Release(&v);
    return out;
}

/* ---- records -------------------------------------------------------------- */

static PyObject *py_record_keyed(PyObject *self, PyObject *args) {
    Py_buffer v;
    Py_ssize_t s, e;
    int r;
    (void)self;
    if (!parse_range(args, &v, &s, &e)) return NULL;
    r = c0_record_keyed(record_of(&v, s, e));
    PyBuffer_Release(&v);
    return PyBool_FromLong(r);
}

static PyObject *py_record_units(PyObject *self, PyObject *args) {
    Py_buffer v;
    Py_ssize_t s, e;
    c0_unit_iter it;
    PyObject *out;
    (void)self;
    if (!parse_range(args, &v, &s, &e)) return NULL;
    it = c0_record_units(record_of(&v, s, e));
    out = drain_units(&it, (const uint8_t *)v.buf);
    PyBuffer_Release(&v);
    return out;
}

/* Keyed units of the range [s, e) (a keyed record or a heading):
 * a list of (key_start, key_end, value_start, value_end). */
static PyObject *py_pairs(PyObject *self, PyObject *args) {
    Py_buffer v;
    Py_ssize_t s, e;
    c0_pair_iter it;
    c0_bytes k, raw;
    const uint8_t *base;
    PyObject *out;
    (void)self;
    if (!parse_range(args, &v, &s, &e)) return NULL;
    base = (const uint8_t *)v.buf;
    it = c0_record_pairs(record_of(&v, s, e));
    out = PyList_New(0);
    if (!out) {
        PyBuffer_Release(&v);
        return NULL;
    }
    while (c0_next_pair(&it, &k, &raw)) {
        Py_ssize_t ks = (Py_ssize_t)(k.ptr - base), vs = (Py_ssize_t)(raw.ptr - base);
        if (append_steal(out, Py_BuildValue("(nnnn)", ks, ks + (Py_ssize_t)k.len, vs,
                                            vs + (Py_ssize_t)raw.len)) != 0) {
            Py_DECREF(out);
            PyBuffer_Release(&v);
            return NULL;
        }
    }
    PyBuffer_Release(&v);
    if (it.err.code) {
        Py_DECREF(out);
        return raise_status(it.err.code, &it.err);
    }
    return out;
}

/* ---- bodies --------------------------------------------------------------- */

/* Files of the body [s, e): a list of (name_start, name_end, start, stop). */
static PyObject *py_body_files(PyObject *self, PyObject *args) {
    Py_buffer v;
    Py_ssize_t s, e;
    c0_file_iter it;
    c0_file f;
    PyObject *out;
    (void)self;
    if (!parse_range(args, &v, &s, &e)) return NULL;
    it = c0_body_files(body_of(&v, s, e));
    out = PyList_New(0);
    if (!out) {
        PyBuffer_Release(&v);
        return NULL;
    }
    while (c0_next_file(&it, &f)) {
        if (append_steal(out, Py_BuildValue("(nnnn)", (Py_ssize_t)f.name_start, (Py_ssize_t)f.name_end,
                                            (Py_ssize_t)f.start, (Py_ssize_t)f.stop)) != 0) {
            Py_DECREF(out);
            PyBuffer_Release(&v);
            return NULL;
        }
    }
    PyBuffer_Release(&v);
    if (it.err.code) {
        Py_DECREF(out);
        return raise_status(it.err.code, &it.err);
    }
    return out;
}

static PyObject *drain_groups(c0_group_iter *it) {
    PyObject *out = PyList_New(0);
    c0_group g;
    if (!out) return NULL;
    while (c0_next_group(it, &g)) {
        if (append_steal(out, group_tuple(&g)) != 0) {
            Py_DECREF(out);
            return NULL;
        }
    }
    if (it->err.code) {
        Py_DECREF(out);
        return raise_status(it->err.code, &it->err);
    }
    return out;
}

/* Groups of the file (name_start, name_end, start, stop). */
static PyObject *py_file_groups(PyObject *self, PyObject *args) {
    Py_buffer v;
    Py_ssize_t ns, ne, s, e;
    c0_file f;
    c0_group_iter it;
    PyObject *out;
    (void)self;
    if (!PyArg_ParseTuple(args, "y*nnnn", &v, &ns, &ne, &s, &e)) return NULL;
    if (ns < 0 || ne < ns || ne > v.len || s < 0 || e < s || e > v.len) {
        PyBuffer_Release(&v);
        PyErr_SetString(PyExc_IndexError, "range outside the buffer");
        return NULL;
    }
    f.buf = (const uint8_t *)v.buf;
    f.name_start = (size_t)ns;
    f.name_end = (size_t)ne;
    f.start = (size_t)s;
    f.stop = (size_t)e;
    it = c0_file_groups(f);
    out = drain_groups(&it);
    PyBuffer_Release(&v);
    return out;
}

/* Groups of every file of the body [s, e). */
static PyObject *py_body_groups(PyObject *self, PyObject *args) {
    Py_buffer v;
    Py_ssize_t s, e;
    c0_group_iter it;
    PyObject *out;
    (void)self;
    if (!parse_range(args, &v, &s, &e)) return NULL;
    it = c0_body_groups(body_of(&v, s, e));
    out = drain_groups(&it);
    PyBuffer_Release(&v);
    return out;
}

static PyObject *py_body_bare(PyObject *self, PyObject *args) {
    Py_buffer v;
    Py_ssize_t s, e;
    int r;
    (void)self;
    if (!parse_range(args, &v, &s, &e)) return NULL;
    r = c0_body_bare(body_of(&v, s, e));
    PyBuffer_Release(&v);
    return PyBool_FromLong(r);
}

static PyObject *drain_records(c0_record_iter *it) {
    PyObject *out = PyList_New(0);
    c0_record r;
    if (!out) return NULL;
    while (c0_next_record(it, &r)) {
        if (append_steal(out, Py_BuildValue("(nn)", (Py_ssize_t)r.start, (Py_ssize_t)r.stop)) != 0) {
            Py_DECREF(out);
            return NULL;
        }
    }
    if (it->err.code) {
        Py_DECREF(out);
        return raise_status(it->err.code, &it->err);
    }
    return out;
}

/* Records of a bare body [s, e). */
static PyObject *py_body_records(PyObject *self, PyObject *args) {
    Py_buffer v;
    Py_ssize_t s, e;
    c0_record_iter it;
    PyObject *out;
    (void)self;
    if (!parse_range(args, &v, &s, &e)) return NULL;
    it = c0_body_records(body_of(&v, s, e));
    out = drain_records(&it);
    PyBuffer_Release(&v);
    return out;
}

/* The group's header row as a record span, or None. */
static PyObject *py_group_header(PyObject *self, PyObject *args) {
    Py_buffer v;
    c0_group g;
    c0_record h;
    int has;
    (void)self;
    if (!parse_group(args, &v, &g)) return NULL;
    has = c0_group_header(g, &h);
    PyBuffer_Release(&v);
    if (!has) Py_RETURN_NONE;
    return Py_BuildValue("(nn)", (Py_ssize_t)h.start, (Py_ssize_t)h.stop);
}

static PyObject *py_group_records(PyObject *self, PyObject *args) {
    Py_buffer v;
    c0_group g;
    c0_record_iter it;
    PyObject *out;
    (void)self;
    if (!parse_group(args, &v, &g)) return NULL;
    it = c0_group_records(g);
    out = drain_records(&it);
    PyBuffer_Release(&v);
    return out;
}

/* ---- documents and containers --------------------------------------------- */

static PyObject *document_tuple(const c0_document *d) {
    return Py_BuildValue("(nnOOnnnnI)", (Py_ssize_t)d->start, (Py_ssize_t)d->stop,
                         d->has_syn ? Py_True : Py_False, d->has_eot ? Py_True : Py_False,
                         (Py_ssize_t)d->heading_start, (Py_ssize_t)d->heading_end,
                         (Py_ssize_t)d->body.start, (Py_ssize_t)d->body.stop, d->warnings);
}

/* Read the document at `start`: (start, stop, has_syn, has_eot, heading_start,
 * heading_end, body_start, body_stop, warning_bits). */
static PyObject *py_document(PyObject *self, PyObject *args) {
    Py_buffer v;
    Py_ssize_t start = 0;
    c0_document d;
    c0_error err;
    c0_status st;
    (void)self;
    if (!PyArg_ParseTuple(args, "y*|n", &v, &start)) return NULL;
    if (start < 0 || start > v.len) {
        PyBuffer_Release(&v);
        PyErr_SetString(PyExc_IndexError, "start outside the buffer");
        return NULL;
    }
    st = c0_document_read((const uint8_t *)v.buf, (size_t)v.len, (size_t)start, &d, &err);
    PyBuffer_Release(&v);
    if (st != C0_OK) return raise_status(st, &err);
    return document_tuple(&d);
}

/* Every document of the container: ([document tuples], has_em, em_pos). */
static PyObject *py_documents(PyObject *self, PyObject *args) {
    Py_buffer v;
    c0_doc_iter it;
    c0_document d;
    PyObject *docs, *res;
    (void)self;
    if (!PyArg_ParseTuple(args, "y*", &v)) return NULL;
    docs = PyList_New(0);
    if (!docs) {
        PyBuffer_Release(&v);
        return NULL;
    }
    it = c0_documents((const uint8_t *)v.buf, (size_t)v.len);
    while (c0_next_document(&it, &d)) {
        if (append_steal(docs, document_tuple(&d)) != 0) {
            Py_DECREF(docs);
            PyBuffer_Release(&v);
            return NULL;
        }
    }
    PyBuffer_Release(&v);
    if (it.err.code) {
        Py_DECREF(docs);
        return raise_status(it.err.code, &it.err);
    }
    res = Py_BuildValue("(NOn)", docs, it.has_em ? Py_True : Py_False, (Py_ssize_t)it.em_pos);
    return res;
}

/* The span of a named blob in the tail, or NotFoundError. */
static PyObject *py_blob(PyObject *self, PyObject *args) {
    Py_buffer v, name;
    c0_bytes out;
    c0_error err;
    c0_status st;
    PyObject *res;
    (void)self;
    if (!PyArg_ParseTuple(args, "y*y*", &v, &name)) return NULL;
    st = c0_blob((const uint8_t *)v.buf, (size_t)v.len, (const uint8_t *)name.buf, (size_t)name.len, &out, &err);
    PyBuffer_Release(&name);
    if (st != C0_OK) {
        PyBuffer_Release(&v);
        return raise_status(st, &err);
    }
    res = span((const uint8_t *)v.buf, out);
    PyBuffer_Release(&v);
    return res;
}

/* ---- stream --------------------------------------------------------------- */

/* (committed_end, torn, [(start, end, damaged)]). */
static PyObject *py_stream(PyObject *self, PyObject *args) {
    Py_buffer v;
    const uint8_t *base;
    c0_stream s;
    c0_block_iter bi;
    c0_bytes blk;
    int damaged;
    PyObject *blocks;
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
    while (c0_next_block(&bi, &blk, &damaged)) {
        Py_ssize_t bs = (Py_ssize_t)(blk.ptr - base);
        if (append_steal(blocks, Py_BuildValue("(nnO)", bs, bs + (Py_ssize_t)blk.len,
                                               damaged ? Py_True : Py_False)) != 0) {
            Py_DECREF(blocks);
            PyBuffer_Release(&v);
            return NULL;
        }
    }
    PyBuffer_Release(&v);
    return Py_BuildValue("(nON)", (Py_ssize_t)s.committed_end, s.torn ? Py_True : Py_False, blocks);
}

/* The undamaged committed blocks, markers and payloads removed, as bytes. */
static PyObject *py_stream_undamaged(PyObject *self, PyObject *args) {
    Py_buffer v;
    c0_stream s;
    PyObject *out;
    size_t n;
    (void)self;
    if (!PyArg_ParseTuple(args, "y*", &v)) return NULL;
    s = c0_stream_read((const uint8_t *)v.buf, (size_t)v.len);
    out = PyBytes_FromStringAndSize(NULL, (Py_ssize_t)s.committed_end);
    if (!out) {
        PyBuffer_Release(&v);
        return NULL;
    }
    n = c0_stream_undamaged(&s, (uint8_t *)PyBytes_AS_STRING(out));
    PyBuffer_Release(&v);
    if (_PyBytes_Resize(&out, (Py_ssize_t)n) != 0) return NULL;
    return out;
}

/* ---- pretty form ---------------------------------------------------------- */

static PyObject *py_pretty_format(PyObject *self, PyObject *args) {
    Py_buffer v;
    const char *indent = NULL;
    int mode = C0_PRETTY_COMPACT;
    char *p;
    size_t outlen;
    c0_error err;
    PyObject *out;
    (void)self;
    if (!PyArg_ParseTuple(args, "y*|zi", &v, &indent, &mode)) return NULL;
    if (mode < C0_PRETTY_COMPACT || mode > C0_PRETTY_SPACED) {
        PyBuffer_Release(&v);
        PyErr_SetString(PyExc_ValueError, "mode must be COMPACT, ALIGNED or SPACED");
        return NULL;
    }
    p = c0_pretty_format((const uint8_t *)v.buf, (size_t)v.len, indent, (c0_pretty_mode)mode, &outlen, &err);
    PyBuffer_Release(&v);
    if (!p) return raise_status(err.code ? err.code : C0_ERR_OOM, &err);
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

/* ---- Builder type --------------------------------------------------------- */

typedef struct {
    PyObject_HEAD
    c0_builder b;
    int closed;
} BuilderObject;

static PyObject *Builder_new(PyTypeObject *type, PyObject *args, PyObject *kwds) {
    BuilderObject *self;
    (void)args;
    (void)kwds;
    self = (BuilderObject *)type->tp_alloc(type, 0);
    if (!self) return NULL;
    c0_builder_init(&self->b);
    self->closed = 0;
    return (PyObject *)self;
}

static void Builder_dealloc(BuilderObject *self) {
    if (!self->closed) c0_builder_free(&self->b);
    Py_TYPE(self)->tp_free((PyObject *)self);
}

/* After a write: raise if the builder failed. Returns a new reference to None. */
static PyObject *builder_check(BuilderObject *self) {
    if (self->closed) {
        PyErr_SetString(Exc_Build, "builder is closed");
        return NULL;
    }
    if (self->b.status != C0_OK) return raise_status(self->b.status, NULL);
    Py_RETURN_NONE;
}

static int builder_open(BuilderObject *self) {
    if (self->closed) {
        PyErr_SetString(Exc_Build, "builder is closed");
        return 0;
    }
    return 1;
}

/* A sequence of bytes objects as a c0_bytes array (PyMem_Free it). The
 * sequence object is returned in *seq (a new reference, keep it alive). */
static c0_bytes *bytes_array(PyObject *seqobj, PyObject **seq, size_t *count) {
    Py_ssize_t n, i;
    c0_bytes *arr;
    *seq = PySequence_Fast(seqobj, "expected a sequence of bytes");
    if (!*seq) return NULL;
    n = PySequence_Fast_GET_SIZE(*seq);
    arr = (c0_bytes *)PyMem_Malloc((size_t)(n ? n : 1) * sizeof(c0_bytes));
    if (!arr) {
        Py_CLEAR(*seq);
        PyErr_NoMemory();
        return NULL;
    }
    for (i = 0; i < n; i++) {
        PyObject *item = PySequence_Fast_GET_ITEM(*seq, i);
        char *p;
        Py_ssize_t len;
        if (PyBytes_AsStringAndSize(item, &p, &len) != 0) {
            PyMem_Free(arr);
            Py_CLEAR(*seq);
            return NULL;
        }
        arr[i].ptr = (const uint8_t *)p;
        arr[i].len = (size_t)len;
    }
    *count = (size_t)n;
    return arr;
}

/* ---- primitives: one method per code ---- */

/* Methods taking no argument. */
#define BUILDER_NOARGS_METHOD(pyname, cfunc)                                  \
    static PyObject *Builder_##pyname(BuilderObject *self, PyObject *noargs) {\
        (void)noargs;                                                         \
        if (!builder_open(self)) return NULL;                                 \
        cfunc(&self->b);                                                      \
        return builder_check(self);                                           \
    }

/* Methods taking one optional bytes argument: the bare code, or the code and
 * what follows it. */
#define BUILDER_OPT_BYTES_METHOD(pyname, bare, withval)                       \
    static PyObject *Builder_##pyname(BuilderObject *self, PyObject *args) {  \
        Py_buffer v = {0};                                                    \
        if (!PyArg_ParseTuple(args, "|y*", &v)) return NULL;                  \
        if (!builder_open(self)) {                                            \
            if (v.obj) PyBuffer_Release(&v);                                  \
            return NULL;                                                      \
        }                                                                     \
        if (v.obj) {                                                          \
            withval(&self->b, (const uint8_t *)v.buf, (size_t)v.len);         \
            PyBuffer_Release(&v);                                             \
        } else {                                                              \
            bare(&self->b);                                                   \
        }                                                                     \
        return builder_check(self);                                           \
    }

BUILDER_NOARGS_METHOD(dle, c0_builder_dle)
BUILDER_NOARGS_METHOD(can, c0_builder_can)
BUILDER_NOARGS_METHOD(syn, c0_builder_syn)
BUILDER_NOARGS_METHOD(eot, c0_builder_eot)
BUILDER_OPT_BYTES_METHOD(gs, c0_builder_gs, c0_builder_gs_name)
BUILDER_OPT_BYTES_METHOD(rs, c0_builder_rs, c0_builder_rs_value)
BUILDER_OPT_BYTES_METHOD(us, c0_builder_us, c0_builder_us_value)
BUILDER_OPT_BYTES_METHOD(sub, c0_builder_sub, c0_builder_sub_value)
BUILDER_OPT_BYTES_METHOD(soh, c0_builder_soh, c0_builder_soh_name)
BUILDER_OPT_BYTES_METHOD(bel, c0_builder_bel, c0_builder_bel_text)
BUILDER_OPT_BYTES_METHOD(etb, c0_builder_etb, c0_builder_etb_payload)

/* Methods taking one bytes argument. */
#define BUILDER_BYTES_METHOD(pyname, cfunc)                                   \
    static PyObject *Builder_##pyname(BuilderObject *self, PyObject *args) {  \
        Py_buffer v;                                                          \
        if (!PyArg_ParseTuple(args, "y*", &v)) return NULL;                   \
        if (!builder_open(self)) {                                            \
            PyBuffer_Release(&v);                                             \
            return NULL;                                                      \
        }                                                                     \
        cfunc(&self->b, (const uint8_t *)v.buf, (size_t)v.len);               \
        PyBuffer_Release(&v);                                                 \
        return builder_check(self);                                           \
    }

BUILDER_BYTES_METHOD(fs, c0_builder_fs)
BUILDER_BYTES_METHOD(em, c0_builder_em)
BUILDER_BYTES_METHOD(file, c0_build_file)
BUILDER_BYTES_METHOD(group, c0_build_group)
BUILDER_BYTES_METHOD(field, c0_build_field)
BUILDER_BYTES_METHOD(key_level_open, c0_build_key_level_open)
BUILDER_BYTES_METHOD(ref, c0_build_ref)
BUILDER_BYTES_METHOD(note, c0_build_note)
BUILDER_BYTES_METHOD(block, c0_build_block)
BUILDER_BYTES_METHOD(item, c0_build_item)

static PyObject *Builder_section(BuilderObject *self, PyObject *args) {
    Py_buffer v;
    Py_ssize_t depth = 1;
    if (!PyArg_ParseTuple(args, "y*|n", &v, &depth)) return NULL;
    if (!builder_open(self) || depth < 1) {
        PyBuffer_Release(&v);
        if (depth < 1 && !PyErr_Occurred()) PyErr_SetString(PyExc_ValueError, "depth must be at least 1");
        return NULL;
    }
    c0_build_section(&self->b, (const uint8_t *)v.buf, (size_t)v.len, (size_t)depth);
    PyBuffer_Release(&v);
    return builder_check(self);
}

/* Methods taking one sequence of bytes. */
#define BUILDER_SEQ_METHOD(pyname, cfunc)                                     \
    static PyObject *Builder_##pyname(BuilderObject *self, PyObject *args) {  \
        PyObject *seqobj, *seq;                                               \
        c0_bytes *arr;                                                        \
        size_t n;                                                             \
        if (!PyArg_ParseTuple(args, "O", &seqobj)) return NULL;               \
        if (!builder_open(self)) return NULL;                                 \
        arr = bytes_array(seqobj, &seq, &n);                                  \
        if (!arr) return NULL;                                                \
        cfunc(&self->b, arr, n);                                              \
        PyMem_Free(arr);                                                      \
        Py_DECREF(seq);                                                       \
        return builder_check(self);                                           \
    }

BUILDER_SEQ_METHOD(enq, c0_builder_enq_path)
BUILDER_SEQ_METHOD(header, c0_build_header)
BUILDER_SEQ_METHOD(record, c0_build_record)
BUILDER_SEQ_METHOD(list, c0_build_list)
BUILDER_SEQ_METHOD(ref_path, c0_build_ref_path)

static PyObject *Builder_map(BuilderObject *self, PyObject *args) {
    PyObject *kobj, *vobj, *kseq, *vseq;
    c0_bytes *keys, *values;
    size_t nk, nv;
    if (!PyArg_ParseTuple(args, "OO", &kobj, &vobj)) return NULL;
    if (!builder_open(self)) return NULL;
    keys = bytes_array(kobj, &kseq, &nk);
    if (!keys) return NULL;
    values = bytes_array(vobj, &vseq, &nv);
    if (!values) {
        PyMem_Free(keys);
        Py_DECREF(kseq);
        return NULL;
    }
    if (nk != nv) {
        PyMem_Free(keys);
        PyMem_Free(values);
        Py_DECREF(kseq);
        Py_DECREF(vseq);
        PyErr_SetString(PyExc_ValueError, "keys and values differ in length");
        return NULL;
    }
    c0_build_map(&self->b, keys, values, nk);
    PyMem_Free(keys);
    PyMem_Free(values);
    Py_DECREF(kseq);
    Py_DECREF(vseq);
    return builder_check(self);
}

static PyObject *Builder_key(BuilderObject *self, PyObject *args) {
    Py_buffer k, v;
    if (!PyArg_ParseTuple(args, "y*y*", &k, &v)) return NULL;
    if (!builder_open(self)) {
        PyBuffer_Release(&k);
        PyBuffer_Release(&v);
        return NULL;
    }
    c0_build_key(&self->b, (const uint8_t *)k.buf, (size_t)k.len, (const uint8_t *)v.buf, (size_t)v.len);
    PyBuffer_Release(&k);
    PyBuffer_Release(&v);
    return builder_check(self);
}

static PyObject *Builder_level_open(BuilderObject *self, PyObject *noargs) {
    (void)noargs;
    if (!builder_open(self)) return NULL;
    c0_build_level_open(&self->b);
    return builder_check(self);
}

static PyObject *Builder_level_close(BuilderObject *self, PyObject *noargs) {
    (void)noargs;
    if (!builder_open(self)) return NULL;
    c0_build_level_close(&self->b);
    return builder_check(self);
}

static PyObject *Builder_bytes(BuilderObject *self, PyObject *noargs) {
    c0_bytes b;
    (void)noargs;
    if (!builder_open(self)) return NULL;
    if (self->b.status != C0_OK) return raise_status(self->b.status, NULL);
    b = c0_builder_bytes(&self->b);
    return PyBytes_FromStringAndSize((const char *)b.ptr, (Py_ssize_t)b.len);
}

static PyObject *Builder_depth(BuilderObject *self, PyObject *noargs) {
    (void)noargs;
    if (!builder_open(self)) return NULL;
    return PyLong_FromSize_t(self->b.depth);
}

static PyObject *Builder_close(BuilderObject *self, PyObject *noargs) {
    (void)noargs;
    if (!self->closed) {
        c0_builder_free(&self->b);
        self->closed = 1;
    }
    Py_RETURN_NONE;
}

static PyMethodDef Builder_methods[] = {
    /* primitives: one per code; the bare form writes the code alone */
    {"fs", (PyCFunction)Builder_fs, METH_VARARGS, "FS name; ends any open record."},
    {"gs", (PyCFunction)Builder_gs, METH_VARARGS, "GS and an optional group name; gs(), gs(name) is depth 2."},
    {"rs", (PyCFunction)Builder_rs, METH_VARARGS, "RS and an optional first value: opens a record."},
    {"us", (PyCFunction)Builder_us, METH_VARARGS, "US and an optional value."},
    {"sub", (PyCFunction)Builder_sub, METH_VARARGS, "SUB and an optional value: the new side of a substitution. C0-DIFF only."},
    {"soh", (PyCFunction)Builder_soh, METH_VARARGS, "SOH and an optional key; bare before rs() it opens a header row."},
    {"dle", (PyCFunction)Builder_dle, METH_NOARGS, "DLE: open a level (saves the record state)."},
    {"can", (PyCFunction)Builder_can, METH_NOARGS, "CAN: close the innermost level; BuildError if none is open."},
    {"bel", (PyCFunction)Builder_bel, METH_VARARGS, "BEL and an optional note; bare, the element that follows is dead."},
    {"enq", (PyCFunction)Builder_enq, METH_VARARGS, "ENQ and a reference: a sequence of one to three names."},
    {"syn", (PyCFunction)Builder_syn, METH_NOARGS, "SYN; soh(key), us(value) pairs that follow are the heading."},
    {"eot", (PyCFunction)Builder_eot, METH_NOARGS, "EOT."},
    {"etb", (PyCFunction)Builder_etb, METH_VARARGS, "ETB and an optional plain payload."},
    {"em", (PyCFunction)Builder_em, METH_VARARGS, "EM and the raw tail; nothing may follow."},
    /* composed: built on the primitives, cannot write ill-formed output */
    {"file", (PyCFunction)Builder_file, METH_VARARGS, "FS name."},
    {"group", (PyCFunction)Builder_group, METH_VARARGS, "GS name."},
    {"section", (PyCFunction)Builder_section, METH_VARARGS, "GS x depth, name."},
    {"header", (PyCFunction)Builder_header, METH_VARARGS, "SOH RS name US name ..."},
    {"record", (PyCFunction)Builder_record, METH_VARARGS, "RS and the given units."},
    {"field", (PyCFunction)Builder_field, METH_VARARGS, "The next unit of the open record."},
    {"map", (PyCFunction)Builder_map, METH_VARARGS, "RS and keyed units (keys, values)."},
    {"key", (PyCFunction)Builder_key, METH_VARARGS, "SOH key US value."},
    {"level_open", (PyCFunction)Builder_level_open, METH_NOARGS, "DLE: a nested level as the next unit."},
    {"key_level_open", (PyCFunction)Builder_key_level_open, METH_VARARGS, "SOH key US DLE."},
    {"level_close", (PyCFunction)Builder_level_close, METH_NOARGS, "CAN."},
    {"list", (PyCFunction)Builder_list, METH_VARARGS, "A flat list as the next unit."},
    {"ref", (PyCFunction)Builder_ref, METH_VARARGS, "ENQ name."},
    {"ref_path", (PyCFunction)Builder_ref_path, METH_VARARGS, "ENQ STX seg US seg [US seg] ETX."},
    {"note", (PyCFunction)Builder_note, METH_VARARGS, "BEL and the note."},
    {"block", (PyCFunction)Builder_block, METH_VARARGS, "A paragraph: a record of one unit."},
    {"item", (PyCFunction)Builder_item, METH_VARARGS, "A list item: the next unit."},
    {"bytes", (PyCFunction)Builder_bytes, METH_NOARGS, "The bytes written so far."},
    {"depth", (PyCFunction)Builder_depth, METH_NOARGS, "Open levels."},
    {"close", (PyCFunction)Builder_close, METH_NOARGS, "Free the buffer now; the builder is then unusable."},
    {NULL, NULL, 0, NULL}};

static PyTypeObject BuilderType = {
    PyVarObject_HEAD_INIT(NULL, 0)
    .tp_name = "c0._c0.Builder",
    .tp_basicsize = sizeof(BuilderObject),
    .tp_itemsize = 0,
    .tp_dealloc = (destructor)Builder_dealloc,
    .tp_flags = Py_TPFLAGS_DEFAULT,
    .tp_doc = "Writer of canonical compact C0DATA over a c0_builder.",
    .tp_methods = Builder_methods,
    .tp_new = Builder_new,
};

/* ---- module --------------------------------------------------------------- */

static PyMethodDef c0_methods[] = {
    {"is_assigned", py_is_assigned, METH_VARARGS, "Whether a byte is one of the sixteen assigned codes."},
    {"is_layout", py_is_layout, METH_VARARGS, "Whether a byte is layout outside text (SP, HT, LF, CR)."},
    {"is_text_escaped", py_is_text_escaped, METH_VARARGS, "Whether a byte takes a DLE inside text."},
    {"is_plain", py_is_plain, METH_VARARGS, "Whether a value can be written plain."},
    {"is_name", py_is_name, METH_VARARGS, "Whether bytes are a legal name."},
    {"decode_text", py_decode_text, METH_VARARGS, "Decode the inside of a text (DLE escapes removed)."},
    {"validate", py_validate, METH_VARARGS, "Check a buffer against the grammar; raises on error."},
    {"canonical", py_canonical, METH_VARARGS, "Whether bytes are a canonical unit."},
    {"unit_type", py_unit_type, METH_VARARGS, "Kind of the raw unit [s, e): 0 plain, 1 text, 2 level, 3 ref."},
    {"value", py_value, METH_VARARGS, "Logical value of the raw unit [s, e)."},
    {"unit_level", py_unit_level, METH_VARARGS, "Body range (start, stop) of the level at [s, e)."},
    {"unit_ref", py_unit_ref, METH_VARARGS, "Segment spans of the reference at [s, e)."},
    {"unit_list", py_unit_list, METH_VARARGS, "Item spans of the list at [s, e)."},
    {"record_keyed", py_record_keyed, METH_VARARGS, "Whether the record [s, e) is keyed."},
    {"record_units", py_record_units, METH_VARARGS, "Positional unit spans of the record [s, e)."},
    {"pairs", py_pairs, METH_VARARGS, "Keyed units of [s, e): (ks, ke, vs, ve) each."},
    {"body_files", py_body_files, METH_VARARGS, "Files of the body [s, e): (ns, ne, start, stop) each."},
    {"file_groups", py_file_groups, METH_VARARGS, "Groups of a file: (depth, ns, ne, start, stop) each."},
    {"body_groups", py_body_groups, METH_VARARGS, "Groups of every file of the body [s, e)."},
    {"body_bare", py_body_bare, METH_VARARGS, "Whether the body [s, e) is a bare record stream."},
    {"body_records", py_body_records, METH_VARARGS, "Record spans of a bare body [s, e)."},
    {"group_header", py_group_header, METH_VARARGS, "Header row span of a group, or None."},
    {"group_records", py_group_records, METH_VARARGS, "Record spans of a group."},
    {"document", py_document, METH_VARARGS, "Read the document at start."},
    {"documents", py_documents, METH_VARARGS, "Every document of a container, has_em, em_pos."},
    {"blob", py_blob, METH_VARARGS, "Span of a named blob in the tail."},
    {"stream", py_stream, METH_VARARGS, "(committed_end, torn, [(start, end, damaged)])."},
    {"stream_undamaged", py_stream_undamaged, METH_VARARGS, "Undamaged committed blocks as bytes."},
    {"pretty_format", py_pretty_format, METH_VARARGS, "Format compact bytes as pretty form."},
    {"pretty_parse", py_pretty_parse, METH_VARARGS, "Parse pretty form back to compact bytes."},
    {NULL, NULL, 0, NULL}};

static struct PyModuleDef c0_module = {
    PyModuleDef_HEAD_INIT, "_c0", "C0DATA 1.0 C core binding", -1, c0_methods,
    NULL, NULL, NULL, NULL};

static PyObject *import_exc(PyObject *errors, const char *name, PyObject **slot) {
    *slot = PyObject_GetAttrString(errors, name);
    return *slot;
}

PyMODINIT_FUNC PyInit__c0(void) {
    PyObject *m, *errors;
    errors = PyImport_ImportModule("c0.errors");
    if (!errors) return NULL;
    if (!import_exc(errors, "C0Error", &Exc_C0Error) || !import_exc(errors, "C0SyntaxError", &Exc_Syntax) ||
        !import_exc(errors, "UnassignedCodeError", &Exc_Unassigned) ||
        !import_exc(errors, "UnexpectedEndError", &Exc_UnexpectedEnd) ||
        !import_exc(errors, "UnitTypeError", &Exc_UnitType) || !import_exc(errors, "NotFoundError", &Exc_NotFound) ||
        !import_exc(errors, "BuildError", &Exc_Build)) {
        Py_DECREF(errors);
        return NULL;
    }
    Py_DECREF(errors);
    if (PyType_Ready(&BuilderType) < 0) return NULL;
    m = PyModule_Create(&c0_module);
    if (!m) return NULL;
    Py_INCREF(&BuilderType);
    if (PyModule_AddObject(m, "Builder", (PyObject *)&BuilderType) < 0) {
        Py_DECREF(&BuilderType);
        Py_DECREF(m);
        return NULL;
    }
    PyModule_AddStringConstant(m, "VERSION", C0_VERSION);
    PyModule_AddIntConstant(m, "WARN_NO_SYN", C0_WARN_NO_SYN);
    PyModule_AddIntConstant(m, "WARN_NO_EOT", C0_WARN_NO_EOT);
    PyModule_AddIntConstant(m, "WARN_UNKNOWN_KEY", C0_WARN_UNKNOWN_KEY);
    PyModule_AddIntConstant(m, "WARN_DUPLICATE_KEY", C0_WARN_DUPLICATE_KEY);
    return m;
}
