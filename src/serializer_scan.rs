//! Find a Bolt Serializer in a response value before msgspec encodes it.
//!
//! msgspec encodes each Struct natively, so a Serializer that a container
//! holds would send its write-only fields. `SerializerScanEncoder.encode`
//! scans the value first. It calls the Python dump only when the scan finds a
//! Serializer, so a value with no Serializer costs one native pass.

use pyo3::exceptions::PyAttributeError;
use pyo3::prelude::*;
#[cfg(Py_GIL_DISABLED)]
use pyo3::types::PyList;
use pyo3::types::{
    PyBool, PyByteArray, PyBytes, PyDict, PyFloat, PyFrozenSet, PyInt, PySet, PyString, PyTuple,
};

/// The deepest level that the scan visits. At this level the scan answers
/// true, and the Python walk then meets the recursion limit, as msgspec does.
/// The scan keeps its own stack, so a deep or cyclic value cannot overflow
/// the thread stack.
const MAX_DEPTH: usize = 10_000;

/// A value of an exact builtin type that cannot hold a Serializer.
#[inline]
fn is_exact_leaf(value: &Bound<'_, PyAny>) -> bool {
    value.is_none()
        || value.is_exact_instance_of::<PyString>()
        || value.is_exact_instance_of::<PyInt>()
        || value.is_exact_instance_of::<PyBool>()
        || value.is_exact_instance_of::<PyFloat>()
}

/// `is_exact_leaf` for a borrowed pointer.
///
/// # Safety
///
/// `value` must point to a live Python object.
#[cfg(not(Py_GIL_DISABLED))]
#[inline]
unsafe fn is_exact_leaf_ptr(value: *mut pyo3::ffi::PyObject) -> bool {
    use pyo3::ffi;
    unsafe {
        value == ffi::Py_None()
            || ffi::PyUnicode_CheckExact(value) != 0
            || ffi::PyLong_CheckExact(value) != 0
            || ffi::PyBool_Check(value) != 0
            || ffi::PyFloat_CheckExact(value) != 0
    }
}

/// A value of a subclass of a scalar type, for example a str Enum.
#[inline]
fn is_scalar_subclass(value: &Bound<'_, PyAny>) -> bool {
    value.is_instance_of::<PyString>()
        || value.is_instance_of::<PyInt>()
        || value.is_instance_of::<PyFloat>()
        || value.is_instance_of::<PyBytes>()
        || value.is_instance_of::<PyByteArray>()
}

/// The number of leaf types that one scan keeps.
const LEAF_TYPES: usize = 8;

/// The slot offsets of the fields of a Struct type, which `find_serializer`
/// keeps in its `offsets` dict. A Struct reads each field at its offset with
/// no attribute lookup.
#[pyclass(frozen)]
pub struct FieldOffsets {
    #[cfg_attr(Py_GIL_DISABLED, allow(dead_code))]
    offsets: Box<[pyo3::ffi::Py_ssize_t]>,
}

/// The layout of a member descriptor (`PyMemberDescrObject`) in CPython 3.12
/// to 3.15. The scan reads the offsets of a type from it only on these
/// versions, and it checks each offset against `getattr` first.
#[cfg(not(Py_GIL_DISABLED))]
#[repr(C)]
struct MemberDescr {
    ob_base: pyo3::ffi::PyObject,
    d_type: *mut pyo3::ffi::PyTypeObject,
    d_name: *mut pyo3::ffi::PyObject,
    d_qualname: *mut pyo3::ffi::PyObject,
    d_member: *mut pyo3::ffi::PyMemberDef,
}

/// What the scan does with a child.
#[derive(PartialEq)]
enum Want {
    /// The child holds no Serializer.
    Skip,
    /// The child goes on the stack.
    Visit,
    /// The child is a `struct_type` instance: the scan reads its fields in place.
    Fields,
}

/// The state of one scan: the objects to visit, the last types that hold no
/// Serializer, and the last Struct type with its field offsets.
struct Scan<'a, 'py> {
    stack: Vec<(Bound<'py, PyAny>, usize)>,
    kinds: &'a Bound<'py, PyDict>,
    classify: &'a Bound<'py, PyAny>,
    /// Types that `kinds` marks as leaves. A child of such a type is
    /// skipped with pointer compares, so a list of datetimes reads the kind
    /// of `datetime` once. The value holds an instance of each type during
    /// the scan, so the types stay alive.
    leaf_types: [*mut pyo3::ffi::PyTypeObject; LEAF_TYPES],
    next_leaf: usize,
    /// The cache of field offsets, keyed by type. Without it, the scan reads
    /// each field with `getattr`.
    #[cfg_attr(Py_GIL_DISABLED, allow(dead_code))]
    offsets: Option<&'a Bound<'py, PyDict>>,
    /// The last Struct type with known offsets. A child of this type has its
    /// fields read in place, so it does not go on the stack.
    #[cfg_attr(Py_GIL_DISABLED, allow(dead_code))]
    struct_type: *mut pyo3::ffi::PyTypeObject,
    #[cfg_attr(Py_GIL_DISABLED, allow(dead_code))]
    struct_offsets: Vec<pyo3::ffi::Py_ssize_t>,
}

impl<'a, 'py> Scan<'a, 'py> {
    #[inline]
    fn is_leaf_type(&self, item_type: *mut pyo3::ffi::PyTypeObject) -> bool {
        self.leaf_types.contains(&item_type)
    }

    fn add_leaf_type(&mut self, item_type: *mut pyo3::ffi::PyTypeObject) {
        self.leaf_types[self.next_leaf] = item_type;
        self.next_leaf = (self.next_leaf + 1) % LEAF_TYPES;
    }

    /// Return what the scan does with an object of type `item_type` that is
    /// not an exact leaf. A type that `kinds` marks as a leaf joins
    /// `leaf_types`. With `switch_struct`, a Struct type with cached offsets
    /// becomes `struct_type`. The kind lookup and `classify` can run Python
    /// code, so the caller holds a strong reference to the object.
    #[inline]
    fn want(&mut self, item_type: *mut pyo3::ffi::PyTypeObject, switch_struct: bool) -> Want {
        use pyo3::ffi;
        // The exact builtin containers come first: they are the most common
        // children, and they take one pointer compare each.
        if item_type == &raw mut ffi::PyDict_Type
            || item_type == &raw mut ffi::PyList_Type
            || item_type == &raw mut ffi::PyTuple_Type
        {
            return Want::Visit;
        }
        if item_type == self.struct_type {
            return Want::Fields;
        }
        if self.is_leaf_type(item_type) {
            return Want::Skip;
        }
        // SAFETY: `item_type` is the type of a live object, so it is alive.
        // The flag reads and the subtype checks run no Python code.
        unsafe {
            let flags = ffi::PyType_GetFlags(item_type);
            let is_subtype =
                |base: *mut ffi::PyTypeObject| ffi::PyType_IsSubtype(item_type, base) != 0;
            // A dict, list, tuple, set or frozenset, also a subclass: `visit`
            // walks its items.
            if flags
                & (ffi::Py_TPFLAGS_DICT_SUBCLASS
                    | ffi::Py_TPFLAGS_LIST_SUBCLASS
                    | ffi::Py_TPFLAGS_TUPLE_SUBCLASS)
                != 0
                || is_subtype(&raw mut ffi::PySet_Type)
                || is_subtype(&raw mut ffi::PyFrozenSet_Type)
            {
                return Want::Visit;
            }
            // A subclass of a scalar type, for example a str Enum.
            if flags
                & (ffi::Py_TPFLAGS_UNICODE_SUBCLASS
                    | ffi::Py_TPFLAGS_LONG_SUBCLASS
                    | ffi::Py_TPFLAGS_BYTES_SUBCLASS)
                != 0
                || is_subtype(&raw mut ffi::PyFloat_Type)
                || is_subtype(&raw mut ffi::PyByteArray_Type)
            {
                self.add_leaf_type(item_type);
                return Want::Skip;
            }
        }
        // SAFETY: `item_type` is the type of a live object, so it is alive.
        let item_type_object =
            unsafe { Bound::from_borrowed_ptr(self.kinds.py(), item_type.cast()) };
        // An error goes to `visit`, which reads the kind again and raises it.
        let kind = match self.kinds.get_item(&item_type_object) {
            Ok(Some(kind)) => kind,
            Ok(None) => match self.classify.call1((&item_type_object,)) {
                Ok(kind) => kind,
                Err(_) => return Want::Visit,
            },
            Err(_) => return Want::Visit,
        };
        if kind.is_none() {
            self.add_leaf_type(item_type);
            return Want::Skip;
        }
        #[cfg(not(Py_GIL_DISABLED))]
        if switch_struct
            && kind.is_instance_of::<PyTuple>()
            && self.use_cached_offsets(&item_type_object)
        {
            return Want::Fields;
        }
        #[cfg(Py_GIL_DISABLED)]
        let _ = switch_struct;
        // A Serializer, or a type with fields to read with getattr.
        Want::Visit
    }

    /// Make `item_type` the `struct_type` if the `offsets` cache holds its
    /// offsets. `visit` fills the cache from the first instance of a type.
    #[cfg(not(Py_GIL_DISABLED))]
    fn use_cached_offsets(&mut self, item_type: &Bound<'py, PyAny>) -> bool {
        let Some(cache) = self.offsets else {
            return false;
        };
        let Ok(Some(cached)) = cache.get_item(item_type) else {
            return false;
        };
        let Ok(cached) = cached.cast::<FieldOffsets>() else {
            return false;
        };
        self.struct_type = item_type.as_ptr().cast();
        self.struct_offsets.clear();
        self.struct_offsets.extend_from_slice(&cached.get().offsets);
        true
    }

    #[inline]
    fn push(&mut self, child: Bound<'py, PyAny>, depth: usize) {
        if !is_exact_leaf(&child) {
            self.push_non_leaf(child, depth);
        }
    }

    /// `push` for a child that is not an exact leaf.
    #[inline]
    fn push_non_leaf(&mut self, child: Bound<'py, PyAny>, depth: usize) {
        match self.want(child.get_type().as_type_ptr(), true) {
            Want::Skip => {}
            Want::Visit => self.stack.push((child, depth)),
            #[cfg(not(Py_GIL_DISABLED))]
            // SAFETY: `child` is a strong reference of type `struct_type`.
            Want::Fields => unsafe { self.push_fields(child.py(), child.as_ptr(), depth) },
            #[cfg(Py_GIL_DISABLED)]
            Want::Fields => self.stack.push((child, depth)),
        }
    }

    /// Put the fields of `item`, an instance of `struct_type`, on the stack.
    ///
    /// A field that is not a leaf gets its kind here, so a `datetime` field
    /// costs one kind lookup per scan, not one visit per instance. That
    /// lookup can run Python code: the strong reference keeps `item` alive,
    /// and each field is read again from it. The lookup does not change
    /// `struct_type`, so the offsets stay those of `item`.
    ///
    /// # Safety
    ///
    /// The thread holds the GIL, `item` is alive, and its type is
    /// `struct_type`.
    #[cfg(not(Py_GIL_DISABLED))]
    unsafe fn push_fields(
        &mut self,
        py: Python<'py>,
        item: *mut pyo3::ffi::PyObject,
        depth: usize,
    ) {
        use pyo3::ffi;
        // SAFETY: the caller keeps `item` alive until this reference exists.
        let item = unsafe { Bound::from_borrowed_ptr(py, item) };
        for index in 0..self.struct_offsets.len() {
            // SAFETY: `offsets_for` checked each offset against the layout of
            // the type and against `getattr` of an instance. `item` is alive.
            let field = unsafe {
                let field = *item
                    .as_ptr()
                    .cast::<u8>()
                    .offset(self.struct_offsets[index])
                    .cast::<*mut ffi::PyObject>();
                if field.is_null()
                    || is_exact_leaf_ptr(field)
                    || self.is_leaf_type(ffi::Py_TYPE(field))
                {
                    continue;
                }
                Bound::from_borrowed_ptr(py, field)
            };
            if self.want(field.get_type().as_type_ptr(), false) != Want::Skip {
                self.stack.push((field, depth));
            }
        }
    }

    /// Return the slot offsets of the fields `names` of the type of `item`,
    /// or None if a field is not a plain slot. The result goes into the
    /// `offsets` cache, also when it is None.
    #[cfg(not(Py_GIL_DISABLED))]
    fn offsets_for(
        &self,
        item: &Bound<'py, PyAny>,
        names: &Bound<'py, PyTuple>,
    ) -> PyResult<Option<Vec<pyo3::ffi::Py_ssize_t>>> {
        let Some(cache) = self.offsets else {
            return Ok(None);
        };
        let item_type = item.get_type();
        if let Some(cached) = cache.get_item(&item_type)? {
            return Ok(cached
                .cast::<FieldOffsets>()
                .ok()
                .map(|cached| cached.get().offsets.to_vec()));
        }
        let offsets = read_field_offsets(item, names)?;
        if cache.len() < MAX_CACHED_TYPES {
            match &offsets {
                Some(offsets) => {
                    let cached = FieldOffsets {
                        offsets: offsets.clone().into_boxed_slice(),
                    };
                    cache.set_item(&item_type, Py::new(item.py(), cached)?)?;
                }
                None => cache.set_item(&item_type, item.py().None())?,
            }
        }
        Ok(offsets)
    }

    /// Put the items of a dict, list or tuple on the stack. Return false for
    /// any other value.
    ///
    /// With the GIL, the loops read borrowed pointers, so a leaf item costs no
    /// reference count change.
    #[cfg(not(Py_GIL_DISABLED))]
    fn push_items(&mut self, item: &Bound<'py, PyAny>, depth: usize) -> bool {
        use pyo3::ffi;
        let py = item.py();
        let container = item.as_ptr();
        // SAFETY: the thread holds the GIL. The leaf checks and the field
        // reads of `push_fields` run no Python code, so the borrowed item
        // stays alive for them. A kind lookup can run Python code: the
        // metaclass of the item can hash in Python, and that code can change
        // the container. Thus each item takes a strong reference before its
        // lookup, the list loop reads the size again for each index, and it
        // stops on a missing item.
        unsafe {
            if ffi::PyDict_Check(container) != 0 {
                let mut position: ffi::Py_ssize_t = 0;
                let mut key = std::ptr::null_mut();
                let mut value = std::ptr::null_mut();
                // PyDict_Next checks its position against the current table,
                // so a dict that changes during a lookup cannot crash it.
                while ffi::PyDict_Next(container, &mut position, &mut key, &mut value) != 0 {
                    if is_exact_leaf_ptr(value) || self.is_leaf_type(ffi::Py_TYPE(value)) {
                        continue;
                    }
                    if ffi::Py_TYPE(value) == self.struct_type {
                        self.push_fields(py, value, depth);
                        continue;
                    }
                    let child = Bound::from_borrowed_ptr(py, value);
                    self.push_non_leaf(child, depth);
                }
                return true;
            }
            let (size, get_item): (
                unsafe extern "C" fn(*mut ffi::PyObject) -> ffi::Py_ssize_t,
                unsafe extern "C" fn(*mut ffi::PyObject, ffi::Py_ssize_t) -> *mut ffi::PyObject,
            ) = if ffi::PyList_Check(container) != 0 {
                (ffi::PyList_Size, ffi::PyList_GetItem)
            } else if ffi::PyTuple_Check(container) != 0 {
                (ffi::PyTuple_Size, ffi::PyTuple_GetItem)
            } else {
                return false;
            };
            let mut index = 0;
            while index < size(container) {
                let child = get_item(container, index);
                if child.is_null() {
                    // The list became shorter during a kind lookup.
                    ffi::PyErr_Clear();
                    break;
                }
                index += 1;
                if is_exact_leaf_ptr(child) || self.is_leaf_type(ffi::Py_TYPE(child)) {
                    continue;
                }
                if ffi::Py_TYPE(child) == self.struct_type {
                    self.push_fields(py, child, depth);
                    continue;
                }
                let child = Bound::from_borrowed_ptr(py, child);
                self.push_non_leaf(child, depth);
            }
        }
        true
    }

    /// Put the items of a dict, list or tuple on the stack. Return false for
    /// any other value.
    ///
    /// Without the GIL, another thread can change a container. The PyO3
    /// iterators hold a strong reference to each item.
    #[cfg(Py_GIL_DISABLED)]
    fn push_items(&mut self, item: &Bound<'py, PyAny>, depth: usize) -> bool {
        if let Ok(dict) = item.cast::<PyDict>() {
            dict.iter().for_each(|(_, child)| self.push(child, depth));
        } else if let Ok(list) = item.cast::<PyList>() {
            list.iter().for_each(|child| self.push(child, depth));
        } else if let Ok(tuple) = item.cast::<PyTuple>() {
            tuple.iter().for_each(|child| self.push(child, depth));
        } else {
            return false;
        }
        true
    }

    /// Put the children of `item` on the stack at `depth`. Return true if
    /// `item` is a Serializer.
    fn visit(&mut self, item: &Bound<'py, PyAny>, depth: usize) -> PyResult<bool> {
        if self.push_items(item, depth) {
            return Ok(false);
        }
        if let Ok(set) = item.cast::<PySet>() {
            set.iter().for_each(|child| self.push(child, depth));
            return Ok(false);
        }
        if let Ok(set) = item.cast::<PyFrozenSet>() {
            set.iter().for_each(|child| self.push(child, depth));
            return Ok(false);
        }
        let item_type = item.get_type();
        if is_scalar_subclass(item) {
            self.add_leaf_type(item_type.as_type_ptr());
            return Ok(false);
        }
        let kind = match self.kinds.get_item(&item_type)? {
            Some(kind) => kind,
            None => self.classify.call1((&item_type,))?,
        };
        if kind.is_none() {
            self.add_leaf_type(item_type.as_type_ptr());
            return Ok(false);
        }
        let Ok(names) = kind.cast::<PyTuple>() else {
            return Ok(true);
        };
        #[cfg(not(Py_GIL_DISABLED))]
        if let Some(offsets) = self.offsets_for(item, names)? {
            self.struct_type = item_type.as_type_ptr();
            self.struct_offsets = offsets;
            // SAFETY: the stack holds `item`, and its type is `struct_type`.
            unsafe { self.push_fields(item.py(), item.as_ptr(), depth) };
            return Ok(false);
        }
        for name in names.iter() {
            match item.getattr(name.cast::<PyString>()?) {
                Ok(child) => self.push(child, depth),
                // msgspec skips an attribute that the object does not have.
                Err(err) if err.is_instance_of::<PyAttributeError>(item.py()) => {}
                Err(err) => return Err(err),
            }
        }
        Ok(false)
    }
}

/// The maximum number of types in an `offsets` cache, as for the kinds.
#[cfg(not(Py_GIL_DISABLED))]
const MAX_CACHED_TYPES: usize = 4096;

/// Read the slot offset of each field of the type of `item` from its member
/// descriptor. Return None if a field is not a plain object slot, or if the
/// Python version is not one whose descriptor layout `MemberDescr` matches.
/// Each offset must give the object that `getattr` gives on `item`.
#[cfg(not(Py_GIL_DISABLED))]
fn read_field_offsets(
    item: &Bound<'_, PyAny>,
    names: &Bound<'_, PyTuple>,
) -> PyResult<Option<Vec<pyo3::ffi::Py_ssize_t>>> {
    use pyo3::ffi;
    let py = item.py();
    if py.version_info() >= (3, 16) {
        return Ok(None);
    }
    let item_type = item.get_type();
    let basicsize: ffi::Py_ssize_t = item_type
        .getattr(pyo3::intern!(py, "__basicsize__"))?
        .extract()?;
    let pointer_size = std::mem::size_of::<*mut ffi::PyObject>() as ffi::Py_ssize_t;
    let header_size = std::mem::size_of::<ffi::PyObject>() as ffi::Py_ssize_t;
    let mut offsets = Vec::with_capacity(names.len());
    for name in names.iter() {
        let name = name.cast::<PyString>()?;
        let descriptor = match item_type.getattr(name) {
            Ok(descriptor) => descriptor,
            Err(err) if err.is_instance_of::<PyAttributeError>(py) => return Ok(None),
            Err(err) => return Err(err),
        };
        if descriptor.get_type().as_type_ptr() != &raw mut ffi::PyMemberDescr_Type {
            return Ok(None);
        }
        // SAFETY: the object is a member descriptor, and `MemberDescr` is its
        // layout on the Python versions checked above.
        let member = unsafe { (*descriptor.as_ptr().cast::<MemberDescr>()).d_member };
        if member.is_null() {
            return Ok(None);
        }
        // SAFETY: a live member descriptor points to its member definition.
        let (type_code, offset, flags) =
            unsafe { ((*member).type_code, (*member).offset, (*member).flags) };
        if type_code != ffi::Py_T_OBJECT_EX
            || flags & ffi::Py_RELATIVE_OFFSET != 0
            || offset < header_size
            || offset % pointer_size != 0
            || offset + pointer_size > basicsize
        {
            return Ok(None);
        }
        // SAFETY: the offset is a pointer-aligned slot inside the instance.
        let field = unsafe {
            *item
                .as_ptr()
                .cast::<u8>()
                .offset(offset)
                .cast::<*mut ffi::PyObject>()
        };
        let same = match item.getattr(name) {
            Ok(value) => value.as_ptr() == field,
            Err(err) if err.is_instance_of::<PyAttributeError>(py) => field.is_null(),
            Err(err) => return Err(err),
        };
        if !same {
            return Ok(None);
        }
        offsets.push(offset);
    }
    Ok(Some(offsets))
}

/// Return whether `value` holds an object whose kind marks a Serializer.
///
/// The scan visits the items of dicts, lists, tuples, sets and frozensets.
/// For each other object, `kinds` maps its type to a kind:
///
/// - `None`: the scan does not visit the object.
/// - a tuple of attribute names: the scan visits these attributes.
/// - any other value (Python gives `True`): the object is a Serializer.
///
/// `classify(type)` gives the kind of a type that `kinds` does not hold.
/// `offsets`, if given, keeps the slot offsets of the fields of each Struct
/// type, so the scan reads them without an attribute lookup.
#[pyfunction]
#[pyo3(signature = (value, kinds, classify, offsets=None))]
pub fn find_serializer<'py>(
    value: &Bound<'py, PyAny>,
    kinds: &Bound<'py, PyDict>,
    classify: &Bound<'py, PyAny>,
    offsets: Option<&Bound<'py, PyDict>>,
) -> PyResult<bool> {
    if is_exact_leaf(value) {
        return Ok(false);
    }
    // The stack allocates only when the value has a child to visit.
    let mut scan = Scan {
        stack: Vec::new(),
        kinds,
        classify,
        leaf_types: [std::ptr::null_mut(); LEAF_TYPES],
        next_leaf: 0,
        offsets,
        struct_type: std::ptr::null_mut(),
        struct_offsets: Vec::new(),
    };
    if scan.visit(value, 1)? {
        return Ok(true);
    }
    while let Some((item, depth)) = scan.stack.pop() {
        if depth >= MAX_DEPTH || scan.visit(&item, depth + 1)? {
            return Ok(true);
        }
    }
    Ok(false)
}

/// A JSON encoder that dumps each Serializer in the value first.
///
/// `encode(value)` scans the value. If the value holds a Serializer, it
/// encodes `dump(value)`. Otherwise it encodes the value as it is.
#[pyclass(frozen)]
pub struct SerializerScanEncoder {
    encode: Py<PyAny>,
    kinds: Py<PyDict>,
    classify: Py<PyAny>,
    dump: Py<PyAny>,
    offsets: Py<PyDict>,
}

#[pymethods]
impl SerializerScanEncoder {
    #[new]
    fn new(
        encode: Py<PyAny>,
        kinds: Py<PyDict>,
        classify: Py<PyAny>,
        dump: Py<PyAny>,
        offsets: Py<PyDict>,
    ) -> Self {
        Self {
            encode,
            kinds,
            classify,
            dump,
            offsets,
        }
    }

    fn encode<'py>(&self, value: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyAny>> {
        let py = value.py();
        let encode = self.encode.bind(py);
        if find_serializer(
            value,
            self.kinds.bind(py),
            self.classify.bind(py),
            Some(self.offsets.bind(py)),
        )? {
            return encode.call1((self.dump.bind(py).call1((value,))?,));
        }
        encode.call1((value,))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use proptest::prelude::*;
    use pyo3::types::{PyList, PyModule};

    const FIXTURES: &std::ffi::CStr = c"
import collections

Pair = collections.namedtuple('Pair', 'left right')

class SetOf(set):
    pass

class FrozenOf(frozenset):
    pass

class Secret:
    pass

class Holder:
    def __init__(self, inner):
        self.inner = inner

class Empty:
    pass

class Text(str):
    pass

calls = []
KINDS = {Holder: ('inner', 'missing')}

def classify(tp):
    calls.append(tp)
    if tp is Secret:
        kind = True
    elif tp is Holder:
        kind = ('inner', 'missing')
    else:
        kind = None
    KINDS[tp] = kind
    return kind

def kinds():
    return KINDS
";

    /// Run the fixtures in a new module for each call.
    ///
    /// `PyModule::from_code` runs the code in the module of `sys.modules`
    /// with the same name. Tests run in parallel threads, so a test could
    /// then replace the classes of another test while it runs.
    fn fixtures(py: Python<'_>) -> Bound<'_, PyModule> {
        let module = PyModule::new(py, "scan_fixtures").unwrap();
        py.run(FIXTURES, Some(&module.dict()), None).unwrap();
        module
    }

    fn scan(module: &Bound<'_, PyModule>, value: &Bound<'_, PyAny>) -> PyResult<bool> {
        let kinds = module.getattr("kinds").unwrap().call0().unwrap();
        let classify = module.getattr("classify").unwrap();
        find_serializer(value, kinds.cast::<PyDict>().unwrap(), &classify, None)
    }

    fn eval<'py>(module: &Bound<'py, PyModule>, expr: &str) -> Bound<'py, PyAny> {
        let code = std::ffi::CString::new(expr).unwrap();
        module.py().eval(&code, Some(&module.dict()), None).unwrap()
    }

    #[test]
    fn finds_a_serializer_in_each_container() {
        Python::initialize();
        Python::attach(|py| {
            let module = fixtures(py);
            for expr in [
                "Secret()",
                "{'a': Secret()}",
                "[1, 'x', {'b': [Secret()]}]",
                "(None, Secret())",
                "{'s': frozenset([Secret()])}",
                "[{Secret()}]",
                "Holder(Secret())",
                "[Holder(Holder(Secret()))]",
                "{'k': Holder({'deep': (Secret(),)})}",
                "collections.OrderedDict(a=Secret())",
                "[Pair(1, Secret())]",
                "{'s': SetOf([Secret()])}",
                "[FrozenOf([Secret()])]",
                "Holder(collections.OrderedDict(a=[Secret()]))",
            ] {
                assert!(scan(&module, &eval(&module, expr)).unwrap(), "{expr}");
            }
        });
    }

    #[test]
    fn finds_no_serializer_in_plain_values() {
        Python::initialize();
        Python::attach(|py| {
            let module = fixtures(py);
            for expr in [
                "None",
                "1",
                "'x'",
                "Text('x')",
                "b'raw'",
                "[1, 2.5, True, None, 'x', Text('y')]",
                "{'a': {'b': [(), set(), frozenset()]}}",
                "Empty()",
                "[Empty(), {'e': Empty()}]",
                "Holder(1)",
                "Holder(Empty())",
            ] {
                assert!(!scan(&module, &eval(&module, expr)).unwrap(), "{expr}");
            }
        });
    }

    #[test]
    fn asks_classify_only_for_a_type_that_kinds_does_not_hold() {
        Python::initialize();
        Python::attach(|py| {
            let module = fixtures(py);
            let value = eval(&module, "[Holder(Empty()), Text('x'), 1, {'a': None}]");
            assert!(!scan(&module, &value).unwrap());
            let calls = eval(&module, "[tp.__name__ for tp in calls]");
            assert_eq!(calls.extract::<Vec<String>>().unwrap(), vec!["Empty"]);
        });
    }

    #[test]
    fn a_leaf_type_hides_no_other_type() {
        Python::initialize();
        Python::attach(|py| {
            let module = fixtures(py);
            let value = eval(&module, "[Empty(), Empty(), {'e': Empty()}, (Empty(),)]");
            assert!(!scan(&module, &value).unwrap());
            let calls = eval(&module, "[tp.__name__ for tp in calls]");
            assert_eq!(calls.extract::<Vec<String>>().unwrap(), vec!["Empty"]);
            for expr in [
                "[Empty(), Empty(), Secret()]",
                "[Empty(), Holder(Secret())]",
            ] {
                assert!(scan(&module, &eval(&module, expr)).unwrap(), "{expr}");
            }
        });
    }

    #[test]
    fn a_container_that_changes_during_a_kind_lookup_does_not_crash_the_scan() {
        Python::initialize();
        Python::attach(|py| {
            let module = fixtures(py);
            // The kind lookup hashes the type. This metaclass runs Python
            // code there that empties the list under scan.
            let code = c"
class Clearing(type):
    def __hash__(cls):
        items.clear()
        return type.__hash__(cls)

class Item(metaclass=Clearing):
    pass

items = [Item(), Item(), Item(), Secret()]
by_key = {'a': Item(), 'b': Item()}
";
            py.run(code, Some(&module.dict()), None).unwrap();
            let items = eval(&module, "items");
            scan(&module, &items).unwrap();
            assert_eq!(items.len().unwrap(), 0);
            let by_key = eval(&module, "by_key");
            assert!(!scan(&module, &by_key).unwrap());
        });
    }

    /// Scan `value` with an offsets cache, as the encoder does.
    fn scan_with_offsets<'py>(
        module: &Bound<'py, PyModule>,
        value: &Bound<'py, PyAny>,
        offsets: &Bound<'py, PyDict>,
    ) -> PyResult<bool> {
        let kinds = module.getattr("kinds").unwrap().call0().unwrap();
        let classify = module.getattr("classify").unwrap();
        find_serializer(
            value,
            kinds.cast::<PyDict>().unwrap(),
            &classify,
            Some(offsets),
        )
    }

    #[test]
    fn a_slotted_type_reads_its_fields_at_their_offsets() {
        Python::initialize();
        Python::attach(|py| {
            let module = fixtures(py);
            let code = c"
class Slotted:
    __slots__ = ('inner', 'tag')
    def __init__(self, inner, tag=None):
        self.inner = inner
        if tag is not None:
            self.tag = tag

class Shadowed:
    __slots__ = ('inner',)
    @property
    def tag(self):
        return Secret()

KINDS[Slotted] = ('inner', 'tag')
KINDS[Shadowed] = ('inner', 'tag')
";
            py.run(code, Some(&module.dict()), None).unwrap();
            let offsets = PyDict::new(py);
            let plain = eval(&module, "[Slotted(1, 'x'), Slotted('y'), Slotted(Empty())]");
            assert!(!scan_with_offsets(&module, &plain, &offsets).unwrap());
            // Without the GIL, the scan reads each field with getattr.
            #[cfg(not(Py_GIL_DISABLED))]
            {
                let slotted = eval(&module, "Slotted");
                let cached = offsets.get_item(&slotted).unwrap().unwrap();
                assert!(cached.cast::<FieldOffsets>().is_ok());
            }
            for expr in [
                "[Slotted(1), Slotted(Secret())]",
                "[Slotted(1), Slotted(2, Secret())]",
                "{'a': Slotted(1), 'b': Slotted([Secret()])}",
                "[Slotted(Slotted(Secret()))]",
            ] {
                let value = eval(&module, expr);
                assert!(
                    scan_with_offsets(&module, &value, &offsets).unwrap(),
                    "{expr}"
                );
            }
            // A property in place of a slot: the type reads its fields with getattr.
            let shadowed = eval(
                &module,
                "(lambda s: (setattr(s, 'inner', 1), s)[1])(Shadowed())",
            );
            assert!(scan_with_offsets(&module, &shadowed, &offsets).unwrap());
            #[cfg(not(Py_GIL_DISABLED))]
            {
                let shadowed_type = eval(&module, "Shadowed");
                assert!(offsets.get_item(&shadowed_type).unwrap().unwrap().is_none());
            }
        });
    }

    #[test]
    fn a_classify_error_reaches_the_caller() {
        Python::initialize();
        Python::attach(|py| {
            let module = fixtures(py);
            let value = eval(&module, "[Empty()]");
            let kinds = PyDict::new(py);
            let classify = eval(&module, "lambda tp: 1 / 0");
            let err = find_serializer(&value, &kinds, &classify, None).unwrap_err();
            assert!(err.is_instance_of::<pyo3::exceptions::PyZeroDivisionError>(py));
        });
    }

    #[test]
    fn a_value_deeper_than_the_limit_counts_as_found() {
        Python::initialize();
        Python::attach(|py| {
            let module = fixtures(py);
            let mut deep = PyList::empty(py).into_any();
            for _ in 0..MAX_DEPTH {
                deep = PyList::new(py, [deep]).unwrap().into_any();
            }
            assert!(scan(&module, &deep).unwrap());
            let cyclic = eval(&module, "(lambda a: (a.append(a), a)[1])([])");
            assert!(scan(&module, &cyclic).unwrap());

            let mut shallow = PyList::empty(py).into_any();
            for _ in 0..MAX_DEPTH - 1 {
                shallow = PyList::new(py, [shallow]).unwrap().into_any();
            }
            assert!(!scan(&module, &shallow).unwrap());
        });
    }

    #[test]
    fn the_encoder_dumps_only_a_value_that_holds_a_serializer() {
        Python::initialize();
        Python::attach(|py| {
            let module = fixtures(py);
            let encoder = SerializerScanEncoder::new(
                eval(&module, "lambda value: ('encoded', value)").unbind(),
                eval(&module, "kinds()")
                    .cast_into::<PyDict>()
                    .unwrap()
                    .unbind(),
                module.getattr("classify").unwrap().unbind(),
                eval(&module, "lambda value: ('dumped', value)").unbind(),
                PyDict::new(py).unbind(),
            );
            let plain = eval(&module, "{'a': [1, Holder(2)]}");
            let encoded = encoder.encode(&plain).unwrap();
            assert!(encoded.get_item(1).unwrap().is(&plain));
            let secret = eval(&module, "{'a': [1, Holder(Secret())]}");
            let encoded = encoder.encode(&secret).unwrap();
            let dumped = encoded.get_item(1).unwrap();
            assert_eq!(
                dumped.get_item(0).unwrap().extract::<String>().unwrap(),
                "dumped"
            );
            assert!(dumped.get_item(1).unwrap().is(&secret));
        });
    }

    /// A JSON-like tree. `Secret` stands for a Serializer.
    #[derive(Clone, Debug)]
    enum Tree {
        Int(i64),
        Text(String),
        Secret,
        Opaque,
        List(Vec<Tree>),
        Tuple(Vec<Tree>),
        // A BTreeMap has unique keys, as a Python dict does.
        Dict(std::collections::BTreeMap<String, Tree>),
        Holder(Box<Tree>),
    }

    impl Tree {
        fn has_secret(&self) -> bool {
            match self {
                Tree::Secret => true,
                Tree::Int(_) | Tree::Text(_) | Tree::Opaque => false,
                Tree::List(items) | Tree::Tuple(items) => items.iter().any(Tree::has_secret),
                Tree::Dict(items) => items.values().any(Tree::has_secret),
                Tree::Holder(inner) => inner.has_secret(),
            }
        }

        fn build<'py>(&self, module: &Bound<'py, PyModule>) -> Bound<'py, PyAny> {
            let py = module.py();
            match self {
                Tree::Int(value) => value.into_pyobject(py).unwrap().into_any(),
                Tree::Text(value) => PyString::new(py, value).into_any(),
                Tree::Secret => module.getattr("Secret").unwrap().call0().unwrap(),
                Tree::Opaque => module.getattr("Empty").unwrap().call0().unwrap(),
                Tree::List(items) => PyList::new(py, items.iter().map(|item| item.build(module)))
                    .unwrap()
                    .into_any(),
                Tree::Tuple(items) => PyTuple::new(py, items.iter().map(|item| item.build(module)))
                    .unwrap()
                    .into_any(),
                Tree::Dict(items) => {
                    let dict = PyDict::new(py);
                    for (key, item) in items {
                        dict.set_item(key, item.build(module)).unwrap();
                    }
                    dict.into_any()
                }
                Tree::Holder(inner) => module
                    .getattr("Holder")
                    .unwrap()
                    .call1((inner.build(module),))
                    .unwrap(),
            }
        }
    }

    fn tree() -> impl Strategy<Value = Tree> {
        let leaf = prop_oneof![
            any::<i64>().prop_map(Tree::Int),
            "[a-z]{0,4}".prop_map(Tree::Text),
            Just(Tree::Opaque),
            Just(Tree::Secret),
        ];
        leaf.prop_recursive(5, 48, 5, |inner| {
            prop_oneof![
                prop::collection::vec(inner.clone(), 0..5).prop_map(Tree::List),
                prop::collection::vec(inner.clone(), 0..5).prop_map(Tree::Tuple),
                prop::collection::btree_map("[a-z]{1,3}", inner.clone(), 0..5).prop_map(Tree::Dict),
                inner.prop_map(|item| Tree::Holder(Box::new(item))),
            ]
        })
    }

    proptest! {
        #[test]
        fn finds_a_serializer_exactly_when_the_tree_holds_one(tree in tree()) {
            Python::initialize();
            Python::attach(|py| {
                let module = fixtures(py);
                let value = tree.build(&module);
                prop_assert_eq!(scan(&module, &value).unwrap(), tree.has_secret());
                Ok(())
            })?;
        }
    }
}
