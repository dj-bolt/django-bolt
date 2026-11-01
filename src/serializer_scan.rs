//! Find a Bolt Serializer in a response value before msgspec encodes it.
//!
//! msgspec encodes each Struct natively, so a Serializer that a container
//! holds would send its write-only fields. `SerializerScanEncoder.encode`
//! scans the value first. It calls the Python dump only when the scan finds a
//! Serializer, so a value with no Serializer costs one native pass.

use pyo3::exceptions::{PyAttributeError, PyRecursionError, PyTypeError};
use pyo3::ffi;
use pyo3::prelude::*;
use pyo3::types::{
    PyBool, PyByteArray, PyBytes, PyDict, PyFloat, PyFrozenSet, PyInt, PyList, PySet, PyString,
    PyTuple, PyType,
};

/// The deepest level that the scan visits. At this level the scan raises
/// RecursionError, as msgspec does for such a value. The scan keeps its own
/// stack, so a deep or cyclic value cannot overflow the thread stack.
const MAX_DEPTH: usize = 10_000;

/// The stack length from which the scan visits each object only once. Only
/// a cycle or a shared object comes back, and it holds the same values. A
/// cycle with a large fan-out thus stops before it fills the memory. A
/// smaller value pays nothing for the check.
const DEDUP_STACK: usize = 100_000;

/// A value of an exact builtin type that cannot hold a Serializer.
#[inline]
fn is_exact_leaf(value: &Bound<'_, PyAny>) -> bool {
    value.is_none()
        || value.is_exact_instance_of::<PyString>()
        || value.is_exact_instance_of::<PyInt>()
        || value.is_exact_instance_of::<PyBool>()
        || value.is_exact_instance_of::<PyFloat>()
}

/// Whether `item_type` is exactly str, int, float or bool.
#[cfg(not(Py_GIL_DISABLED))]
#[inline(always)]
fn is_exact_scalar_type(item_type: *mut ffi::PyTypeObject) -> bool {
    item_type == &raw mut ffi::PyUnicode_Type
        || item_type == &raw mut ffi::PyLong_Type
        || item_type == &raw mut ffi::PyFloat_Type
        || item_type == &raw mut ffi::PyBool_Type
}

/// Whether `item_type` is exactly dict, list or tuple.
#[inline(always)]
fn is_exact_container_type(item_type: *mut ffi::PyTypeObject) -> bool {
    item_type == &raw mut ffi::PyDict_Type
        || item_type == &raw mut ffi::PyList_Type
        || item_type == &raw mut ffi::PyTuple_Type
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

/// Whether `item_type` is a dataclass or an attrs class. msgspec encodes the
/// fields of such a class, also when it is a subclass of a scalar type.
fn has_fields(item_type: &Bound<'_, PyAny>) -> PyResult<bool> {
    let py = item_type.py();
    Ok(
        item_type.hasattr(pyo3::intern!(py, "__dataclass_fields__"))?
            || item_type.hasattr(pyo3::intern!(py, "__attrs_attrs__"))?,
    )
}

/// The getset descriptor `name` of `type` itself, for example `__mro__`.
///
/// A metaclass can override these attributes for its classes. The getset
/// descriptors of `type` read the fields of the type object instead.
fn type_getset<'py>(
    py: Python<'py>,
    cell: &'static pyo3::sync::PyOnceLock<Py<PyAny>>,
    name: &str,
) -> PyResult<Bound<'py, PyAny>> {
    cell.get_or_try_init(py, || {
        py.get_type::<PyType>()
            .getattr(pyo3::intern!(py, "__dict__"))?
            .get_item(name)
            .map(Bound::unbind)
    })
    .map(|getset| getset.bind(py).clone())
}

/// Return the attribute `name` of `item_type` as the type lookup finds it
/// in the dicts of its MRO, or None. A metaclass cannot change the result.
fn type_lookup<'py>(
    item_type: &Bound<'py, PyType>,
    name: &Bound<'py, PyString>,
) -> PyResult<Option<Bound<'py, PyAny>>> {
    static MRO: pyo3::sync::PyOnceLock<Py<PyAny>> = pyo3::sync::PyOnceLock::new();
    static DICT: pyo3::sync::PyOnceLock<Py<PyAny>> = pyo3::sync::PyOnceLock::new();
    let py = item_type.py();
    let get = pyo3::intern!(py, "__get__");
    let mro = type_getset(py, &MRO, "__mro__")?.call_method1(get, (item_type,))?;
    let dict_getset = type_getset(py, &DICT, "__dict__")?;
    for base in mro.cast::<PyTuple>()?.iter() {
        let namespace = dict_getset.call_method1(get, (&base,))?;
        let value = namespace.call_method1(pyo3::intern!(py, "get"), (name,))?;
        if !value.is_none() {
            return Ok(Some(value));
        }
        if namespace.contains(name)? {
            return Ok(Some(value));
        }
    }
    Ok(None)
}

/// The function `msgspec.structs.astuple`, or None without msgspec. It reads
/// the fields of a Struct from the slots that the class had when it was
/// made, as msgspec encodes them. Only a Struct has a kind that is an exact
/// tuple, and a Struct needs msgspec, so the scan has the function for each
/// Struct. The tests of this module have no msgspec.
fn astuple(py: Python<'_>) -> Option<&Bound<'_, PyAny>> {
    static ASTUPLE: pyo3::sync::PyOnceLock<Option<Py<PyAny>>> = pyo3::sync::PyOnceLock::new();
    ASTUPLE
        .get_or_init(py, || {
            py.import("msgspec.structs")
                .and_then(|module| module.getattr("astuple"))
                .map(Bound::unbind)
                .ok()
        })
        .as_ref()
        .map(|function| function.bind(py))
}

/// Return the `__dict__` of `item` if its type uses the attribute lookup of
/// `object` and the object has a dict. msgspec reads a dataclass field from
/// this dict first.
fn instance_dict<'py>(item: &Bound<'py, PyAny>) -> Option<Bound<'py, PyDict>> {
    if !uses_generic_getattr(item.get_type().as_type_ptr()) {
        return None;
    }
    // SAFETY: `item` is a live object. The call gives a new reference or null.
    let dict = unsafe {
        Bound::from_owned_ptr_or_err(
            item.py(),
            ffi::PyObject_GenericGetDict(item.as_ptr(), std::ptr::null_mut()),
        )
    };
    match dict {
        Ok(dict) => dict.cast_into::<PyDict>().ok(),
        Err(_) => None,
    }
}

/// The number of leaf types that one scan keeps.
const LEAF_TYPES: usize = 8;

/// A hasher for object addresses. An address is already unique, so one
/// multiply spreads it over the bits that the hash table reads.
#[derive(Default)]
struct AddressHasher(u64);

impl std::hash::Hasher for AddressHasher {
    fn finish(&self) -> u64 {
        self.0
    }

    fn write(&mut self, bytes: &[u8]) {
        for byte in bytes {
            self.0 = (self.0 ^ u64::from(*byte)).wrapping_mul(0x0100_0000_01b3);
        }
    }

    fn write_usize(&mut self, value: usize) {
        self.0 = (value as u64 >> 4).wrapping_mul(0x9e37_79b9_7f4a_7c15);
    }
}

type AddressSet = std::collections::HashSet<usize, std::hash::BuildHasherDefault<AddressHasher>>;

/// The slot offsets of the fields of a type, which `find_serializer` keeps
/// in its `offsets` dict. The scan reads each field at its offset with no
/// attribute lookup.
///
/// The dict compares its keys with `__eq__`, and a metaclass can make two
/// distinct types equal. Thus the entry records its type, and the scan uses
/// the offsets only for that exact type.
#[pyclass(frozen)]
pub struct FieldOffsets {
    #[cfg_attr(Py_GIL_DISABLED, allow(dead_code))]
    offsets: Box<[ffi::Py_ssize_t]>,
    /// The type of the offsets, as a strong reference. The dict can keep
    /// another key that is equal to it, so the entry itself keeps the type
    /// alive: no other type can then take its address.
    #[cfg_attr(Py_GIL_DISABLED, allow(dead_code))]
    owner: Py<PyAny>,
}

impl FieldOffsets {
    /// Return the offsets if this entry belongs to `item_type`.
    #[cfg(not(Py_GIL_DISABLED))]
    fn for_type(&self, item_type: *mut ffi::PyTypeObject) -> Option<&[ffi::Py_ssize_t]> {
        (self.owner.as_ptr() == item_type.cast()).then_some(&self.offsets)
    }
}

/// The layout of a member descriptor (`PyMemberDescrObject`) in CPython 3.12
/// to 3.15. The scan reads the offsets of a type from it only on these
/// versions, and it checks each offset on an instance first.
#[cfg(not(Py_GIL_DISABLED))]
#[repr(C)]
struct MemberDescr {
    ob_base: ffi::PyObject,
    d_type: *mut ffi::PyTypeObject,
    d_name: *mut ffi::PyObject,
    d_qualname: *mut ffi::PyObject,
    d_member: *mut ffi::PyMemberDef,
}

/// The start of the layout of a list (`PyListObject`) on a GIL build of
/// CPython 3.12 to 3.15. The scan reads the items of a list in place, as the
/// `PyList_GET_ITEM` macro does.
#[cfg(not(Py_GIL_DISABLED))]
#[repr(C)]
struct ListLayout {
    ob_base: ffi::PyObject,
    ob_size: ffi::Py_ssize_t,
    ob_item: *mut *mut ffi::PyObject,
}

/// Return whether this Python has the layouts of `ListLayout` and
/// `MemberDescr`. The check runs once. The runtime must be CPython 3.15 or
/// older, and a sample list must read the same in place as through the C API.
#[cfg(not(Py_GIL_DISABLED))]
fn layouts_known(py: Python<'_>) -> bool {
    static KNOWN: pyo3::sync::PyOnceLock<bool> = pyo3::sync::PyOnceLock::new();
    *KNOWN.get_or_init(py, || {
        let is_cpython = py
            .import("sys")
            .and_then(|sys| sys.getattr("implementation"))
            .and_then(|implementation| implementation.getattr("name"))
            .and_then(|name| name.extract::<String>())
            .is_ok_and(|name| name == "cpython");
        if !is_cpython || py.version_info() >= (3, 16) {
            return false;
        }
        let Ok(sample) = PyList::new(py, [1, 2, 3]) else {
            return false;
        };
        let list = sample.as_ptr();
        // SAFETY: `sample` is a live list of three items, and the reads stay
        // inside it on a layout that matches `ListLayout`.
        unsafe {
            let layout = list.cast::<ListLayout>();
            (*layout).ob_size == 3
                && (0..3).all(|index| {
                    *(*layout).ob_item.offset(index) == ffi::PyList_GetItem(list, index)
                })
        }
    })
}

/// Return whether `item_type` uses the attribute lookup of `object`. msgspec
/// reads a dataclass field with `getattr` when the class has a lookup of its
/// own, so only such a class can have its slots read in place.
fn uses_generic_getattr(item_type: *mut ffi::PyTypeObject) -> bool {
    // SAFETY: `item_type` is a live type, and the slot read runs no Python code.
    let getattro = unsafe { ffi::PyType_GetSlot(item_type, ffi::Py_tp_getattro) };
    getattro == ffi::PyObject_GenericGetAttr as *mut std::ffi::c_void
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
/// Serializer, and the last type whose fields the scan reads at offsets.
struct Scan<'a, 'py> {
    stack: Vec<(Bound<'py, PyAny>, usize)>,
    kinds: &'a Bound<'py, PyDict>,
    classify: &'a Bound<'py, PyAny>,
    /// Types that `kinds` marks as leaves. A child of such a type is
    /// skipped with pointer compares, so a list of datetimes reads the kind
    /// of `datetime` once. The first `leaf_count` entries are strong
    /// references: Python code can run during the scan, so a new type could
    /// otherwise take the address of a freed type.
    leaf_types: [*mut ffi::PyTypeObject; LEAF_TYPES],
    leaf_count: usize,
    next_leaf: usize,
    /// The cache of field offsets, keyed by type. Without it, the scan reads
    /// each field with an attribute lookup.
    #[cfg_attr(Py_GIL_DISABLED, allow(dead_code))]
    offsets: Option<&'a Bound<'py, PyDict>>,
    /// The last type with known offsets, as a strong reference, or null. A
    /// child of this type has its fields read in place, so it does not go on
    /// the stack.
    #[cfg_attr(Py_GIL_DISABLED, allow(dead_code))]
    struct_type: *mut ffi::PyTypeObject,
    #[cfg_attr(Py_GIL_DISABLED, allow(dead_code))]
    struct_offsets: Vec<ffi::Py_ssize_t>,
    /// Whether the scan can read lists and member descriptors in place.
    #[cfg_attr(Py_GIL_DISABLED, allow(dead_code))]
    layouts: bool,
    /// Whether the stack reached `DEDUP_STACK` in this scan.
    dedup: bool,
    /// From then on, the addresses of the objects that the scan visited, with
    /// a strong reference to each, so that no other object can take an
    /// address during the scan.
    seen: AddressSet,
    seen_refs: Vec<Bound<'py, PyAny>>,
}

impl<'a, 'py> Scan<'a, 'py> {
    #[inline]
    fn is_leaf_type(&self, item_type: *mut ffi::PyTypeObject) -> bool {
        self.leaf_types.contains(&item_type)
    }

    /// Add `item_type`, the type of a live object, to `leaf_types`.
    fn add_leaf_type(&mut self, item_type: *mut ffi::PyTypeObject) {
        // SAFETY: the object keeps its type alive until the new reference
        // exists. The entry to replace is a strong reference of the scan.
        unsafe {
            ffi::Py_IncRef(item_type.cast());
            if self.leaf_count == LEAF_TYPES {
                ffi::Py_DecRef(self.leaf_types[self.next_leaf].cast());
            } else {
                self.leaf_count += 1;
            }
        }
        self.leaf_types[self.next_leaf] = item_type;
        self.next_leaf = (self.next_leaf + 1) % LEAF_TYPES;
    }

    /// Make `item_type`, the type of a live object, the `struct_type`.
    #[cfg(not(Py_GIL_DISABLED))]
    fn set_struct_type(&mut self, item_type: *mut ffi::PyTypeObject) {
        // SAFETY: the object keeps its type alive until the new reference
        // exists. A previous `struct_type` is a strong reference of the scan.
        unsafe {
            ffi::Py_IncRef(item_type.cast());
            if !self.struct_type.is_null() {
                ffi::Py_DecRef(self.struct_type.cast());
            }
        }
        self.struct_type = item_type;
    }

    /// Return what the scan does with an object of type `item_type` that is
    /// not an exact leaf. A type that `kinds` marks as a leaf joins
    /// `leaf_types`. With `switch_struct`, a type with cached offsets
    /// becomes `struct_type`. The kind lookup and `classify` can run Python
    /// code, so the caller holds a strong reference to the object.
    #[inline]
    fn want(&mut self, item_type: *mut ffi::PyTypeObject, switch_struct: bool) -> Want {
        // The exact builtin containers come first: they are the most common
        // children, and they take one pointer compare each.
        if is_exact_container_type(item_type) {
            return Want::Visit;
        }
        if item_type == self.struct_type {
            return Want::Fields;
        }
        if self.is_leaf_type(item_type) {
            return Want::Skip;
        }
        // SAFETY: `item_type` is the type of a live object, so it is alive.
        let item_type_object =
            unsafe { Bound::from_borrowed_ptr(self.kinds.py(), item_type.cast()) };
        // An error goes to `visit`, which reads the kind again and raises it.
        let kind = match self.kinds.get_item(&item_type_object) {
            Ok(Some(kind)) => kind,
            Ok(None) => {
                // `classify` never gets a container or a scalar subclass, so
                // only a type that `kinds` does not hold needs these checks.
                if let Some(want) = self.want_by_flags(item_type) {
                    return want;
                }
                match self.classify.call1((&item_type_object,)) {
                    Ok(kind) => kind,
                    Err(_) => return Want::Visit,
                }
            }
            Err(_) => return Want::Visit,
        };
        if kind.is_none() {
            self.add_leaf_type(item_type);
            return Want::Skip;
        }
        #[cfg(not(Py_GIL_DISABLED))]
        if switch_struct
            && kind.is_instance_of::<PyTuple>()
            && self.use_cached_offsets(&item_type_object, !kind.is_exact_instance_of::<PyTuple>())
        {
            return Want::Fields;
        }
        #[cfg(Py_GIL_DISABLED)]
        let _ = switch_struct;
        // A Serializer, or a type with fields to read with an attribute lookup.
        Want::Visit
    }

    /// Return what the scan does with a container or a scalar subclass of
    /// type `item_type`, or None for any other type.
    fn want_by_flags(&mut self, item_type: *mut ffi::PyTypeObject) -> Option<Want> {
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
                return Some(Want::Visit);
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
                // msgspec encodes the fields of a dataclass or an attrs
                // class, also of a subclass of a scalar type. An error goes
                // to `visit`, which gets the kind and raises it.
                let type_object = Bound::from_borrowed_ptr(self.kinds.py(), item_type.cast());
                if has_fields(&type_object).unwrap_or(true) {
                    return None;
                }
                self.add_leaf_type(item_type);
                return Some(Want::Skip);
            }
        }
        None
    }

    /// Make `item_type` the `struct_type` if the `offsets` cache holds its
    /// offsets. `visit` fills the cache from the first instance of a type.
    /// With `generic_only`, the type must still use the attribute lookup of
    /// `object`: an app can give a class its own lookup after a scan.
    #[cfg(not(Py_GIL_DISABLED))]
    fn use_cached_offsets(&mut self, item_type: &Bound<'py, PyAny>, generic_only: bool) -> bool {
        let Some(cache) = self.offsets else {
            return false;
        };
        let Ok(Some(cached)) = cache.get_item(item_type) else {
            return false;
        };
        let Ok(cached) = cached.cast::<FieldOffsets>() else {
            return false;
        };
        let type_ptr: *mut ffi::PyTypeObject = item_type.as_ptr().cast();
        let Some(offsets) = cached.get().for_type(type_ptr) else {
            return false;
        };
        if generic_only && !uses_generic_getattr(type_ptr) {
            return false;
        }
        self.struct_offsets.clear();
        self.struct_offsets.extend_from_slice(offsets);
        self.set_struct_type(type_ptr);
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

    /// `push` for a child that a GIL loop reads as a borrowed pointer.
    ///
    /// The checks before the strong reference run no Python code, so the
    /// child stays alive for them.
    ///
    /// # Safety
    ///
    /// The thread holds the GIL, and `child` points to a live object.
    #[cfg(not(Py_GIL_DISABLED))]
    #[inline(always)]
    unsafe fn push_borrowed(&mut self, py: Python<'py>, child: *mut ffi::PyObject, depth: usize) {
        unsafe {
            let child_type = ffi::Py_TYPE(child);
            // The checks go in the order of how common the types are in JSON.
            if child_type == &raw mut ffi::PyUnicode_Type {
                return;
            }
            if is_exact_container_type(child_type) {
                self.stack
                    .push((Bound::from_borrowed_ptr(py, child), depth));
                return;
            }
            if is_exact_scalar_type(child_type) || child == ffi::Py_None() {
                return;
            }
            if child_type == self.struct_type {
                self.push_fields(py, child, depth);
            } else if !self.is_leaf_type(child_type) {
                self.push_non_leaf(Bound::from_borrowed_ptr(py, child), depth);
            }
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
    unsafe fn push_fields(&mut self, py: Python<'py>, item: *mut ffi::PyObject, depth: usize) {
        // SAFETY: the caller keeps `item` alive until this reference exists.
        let item = unsafe { Bound::from_borrowed_ptr(py, item) };
        for index in 0..self.struct_offsets.len() {
            // SAFETY: `offsets_for` checked each offset against the layout of
            // the type and against an instance. `item` is alive. The checks
            // before the strong reference run no Python code.
            let (field, field_type) = unsafe {
                let field = *item
                    .as_ptr()
                    .cast::<u8>()
                    .offset(self.struct_offsets[index])
                    .cast::<*mut ffi::PyObject>();
                if field.is_null() {
                    continue;
                }
                let field_type = ffi::Py_TYPE(field);
                if is_exact_scalar_type(field_type)
                    || field == ffi::Py_None()
                    || self.is_leaf_type(field_type)
                {
                    continue;
                }
                (Bound::from_borrowed_ptr(py, field), field_type)
            };
            if is_exact_container_type(field_type) || self.want(field_type, false) != Want::Skip {
                self.stack.push((field, depth));
            }
        }
    }

    /// Return the slot offsets of the fields `names` of the type of `item`,
    /// or None if a field is not a plain slot. The result goes into the
    /// `offsets` cache, also when it is None.
    ///
    /// With `generic_only`, a type with its own attribute lookup gets None:
    /// msgspec reads the fields of such a dataclass with `getattr`.
    #[cfg(not(Py_GIL_DISABLED))]
    fn offsets_for(
        &self,
        item: &Bound<'py, PyAny>,
        names: &Bound<'py, PyTuple>,
        generic_only: bool,
    ) -> PyResult<Option<Vec<ffi::Py_ssize_t>>> {
        let Some(cache) = self.offsets else {
            return Ok(None);
        };
        let item_type = item.get_type();
        let type_ptr = item_type.as_type_ptr();
        let cached = match cache.get_item(&item_type) {
            Ok(cached) => cached,
            // A type whose metaclass has __eq__ and no __hash__ cannot be a key.
            Err(err) if err.is_instance_of::<PyTypeError>(item.py()) => return Ok(None),
            Err(err) => return Err(err),
        };
        if let Some(cached) = cached {
            if generic_only && !uses_generic_getattr(type_ptr) {
                return Ok(None);
            }
            return Ok(cached
                .cast::<FieldOffsets>()
                .ok()
                .and_then(|cached| cached.get().for_type(type_ptr).map(<[_]>::to_vec)));
        }
        let offsets = if self.layouts {
            read_field_offsets(item, names, generic_only)?
        } else {
            None
        };
        if cache.len() < MAX_CACHED_TYPES {
            match &offsets {
                Some(offsets) => {
                    let cached = FieldOffsets {
                        offsets: offsets.clone().into_boxed_slice(),
                        owner: item_type.clone().into_any().unbind(),
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
        let py = item.py();
        let container = item.as_ptr();
        // SAFETY: the thread holds the GIL, and the stack holds `item`.
        unsafe {
            let container_type = ffi::Py_TYPE(container);
            if container_type == &raw mut ffi::PyDict_Type {
                self.push_dict_values(py, container, depth);
            } else if container_type == &raw mut ffi::PyList_Type {
                self.push_list_items(py, container, depth);
            } else if container_type == &raw mut ffi::PyTuple_Type {
                self.push_tuple_items(py, container, depth);
            } else if ffi::PyDict_Check(container) != 0 {
                self.push_dict_values(py, container, depth);
            } else if ffi::PyList_Check(container) != 0 {
                self.push_list_items(py, container, depth);
            } else if ffi::PyTuple_Check(container) != 0 {
                self.push_tuple_items(py, container, depth);
            } else {
                return false;
            }
        }
        true
    }

    /// Put the values of `dict` on the stack.
    ///
    /// # Safety
    ///
    /// The thread holds the GIL, and `dict` is a live dict.
    #[cfg(not(Py_GIL_DISABLED))]
    unsafe fn push_dict_values(&mut self, py: Python<'py>, dict: *mut ffi::PyObject, depth: usize) {
        unsafe {
            let mut position: ffi::Py_ssize_t = 0;
            let mut key = std::ptr::null_mut();
            let mut value = std::ptr::null_mut();
            // A kind lookup can run Python code that changes the dict.
            // PyDict_Next checks its position against the current table, so
            // such a change cannot crash it.
            while ffi::PyDict_Next(dict, &mut position, &mut key, &mut value) != 0 {
                self.push_borrowed(py, value, depth);
            }
        }
    }

    /// Put the items of `list` on the stack.
    ///
    /// A kind lookup can run Python code that changes the list. Thus the
    /// loop reads the size and the item array again for each index.
    ///
    /// # Safety
    ///
    /// The thread holds the GIL, and `list` is a live list.
    #[cfg(not(Py_GIL_DISABLED))]
    unsafe fn push_list_items(&mut self, py: Python<'py>, list: *mut ffi::PyObject, depth: usize) {
        let mut index = 0;
        unsafe {
            if self.layouts {
                let layout = list.cast::<ListLayout>();
                while index < (*layout).ob_size {
                    let child = *(*layout).ob_item.offset(index);
                    index += 1;
                    self.push_borrowed(py, child, depth);
                }
                return;
            }
            while index < ffi::PyList_Size(list) {
                let child = ffi::PyList_GetItem(list, index);
                if child.is_null() {
                    ffi::PyErr_Clear();
                    break;
                }
                index += 1;
                self.push_borrowed(py, child, depth);
            }
        }
    }

    /// Put the items of `tuple` on the stack. A tuple does not change, and
    /// it holds its items.
    ///
    /// # Safety
    ///
    /// The thread holds the GIL, and `tuple` is a live tuple.
    #[cfg(not(Py_GIL_DISABLED))]
    unsafe fn push_tuple_items(
        &mut self,
        py: Python<'py>,
        tuple: *mut ffi::PyObject,
        depth: usize,
    ) {
        unsafe {
            for index in 0..ffi::PyTuple_Size(tuple) {
                let child = ffi::PyTuple_GetItem(tuple, index);
                if child.is_null() {
                    ffi::PyErr_Clear();
                    break;
                }
                self.push_borrowed(py, child, depth);
            }
        }
    }

    /// Put the items of a dict, list or tuple on the stack. Return false for
    /// any other value.
    ///
    /// Without the GIL, another thread can change a container. The PyO3
    /// iterators hold a strong reference to each item. The dict iterator of
    /// PyO3 panics when the size of the dict changes, so the loop reads a
    /// copy of the values.
    #[cfg(Py_GIL_DISABLED)]
    fn push_items(&mut self, item: &Bound<'py, PyAny>, depth: usize) -> bool {
        if let Ok(dict) = item.cast::<PyDict>() {
            dict.values()
                .iter()
                .for_each(|child| self.push(child, depth));
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
        if item.is_instance_of::<PySet>() || item.is_instance_of::<PyFrozenSet>() {
            // The set iterator of PyO3 panics when the set changes during the
            // loop. The iterator of Python raises a RuntimeError instead.
            for child in item.try_iter()? {
                self.push(child?, depth);
            }
            return Ok(false);
        }
        let item_type = item.get_type();
        if is_scalar_subclass(item) && !has_fields(&item_type)? {
            self.add_leaf_type(item_type.as_type_ptr());
            return Ok(false);
        }
        let kind = match self.kinds.get_item(&item_type) {
            Ok(Some(kind)) => kind,
            Ok(None) => self.classify.call1((&item_type,))?,
            // A type whose metaclass has __eq__ and no __hash__ cannot be a
            // key, so it gets its kind each time.
            Err(err) if err.is_instance_of::<PyTypeError>(item.py()) => {
                self.classify.call1((&item_type,))?
            }
            Err(err) => return Err(err),
        };
        if kind.is_none() {
            self.add_leaf_type(item_type.as_type_ptr());
            return Ok(false);
        }
        let Ok(names) = kind.cast::<PyTuple>() else {
            return Ok(true);
        };
        // A tuple names the fields of a Struct, which msgspec reads from
        // their slots. A tuple subclass names the fields of another type,
        // which msgspec reads with `getattr` if the type has its own lookup.
        let slots = names.is_exact_instance_of::<PyTuple>();
        #[cfg(not(Py_GIL_DISABLED))]
        if let Some(offsets) = self.offsets_for(item, names, !slots)? {
            self.set_struct_type(item_type.as_type_ptr());
            self.struct_offsets = offsets;
            // SAFETY: the stack holds `item`, and its type is `struct_type`.
            unsafe { self.push_fields(item.py(), item.as_ptr(), depth) };
            return Ok(false);
        }
        if slots && let Some(astuple) = astuple(item.py()) {
            // msgspec reads a Struct field from the slot that the class had
            // when it was made, also when a property now hides the field.
            let values = astuple.call1((item,))?;
            for child in values.cast::<PyTuple>()?.iter() {
                self.push(child, depth);
            }
            return Ok(false);
        }
        // msgspec reads a field of a dataclass or an attrs class from the
        // `__dict__` of the object first, if the type uses the lookup of
        // `object`. Otherwise it uses `getattr`.
        let dict = if slots { None } else { instance_dict(item) };
        for name in names.iter() {
            let name = name.cast::<PyString>()?;
            let child = match &dict {
                Some(dict) => match dict.get_item(name)? {
                    Some(child) => Ok(child),
                    None => slot_value(item, name),
                },
                None if slots => generic_getattr(item, name),
                None => item.getattr(name),
            };
            match child {
                Ok(child) => self.push(child, depth),
                // msgspec skips an attribute that the object does not have.
                Err(err) if err.is_instance_of::<PyAttributeError>(item.py()) => {}
                Err(err) => return Err(err),
            }
        }
        Ok(false)
    }
}

impl Drop for Scan<'_, '_> {
    fn drop(&mut self) {
        // SAFETY: the scan holds a strong reference to each of these types.
        unsafe {
            for item_type in &self.leaf_types[..self.leaf_count] {
                ffi::Py_DecRef(item_type.cast());
            }
            if !self.struct_type.is_null() {
                ffi::Py_DecRef(self.struct_type.cast());
            }
        }
    }
}

/// Read the attribute `name` of `item` with the lookup of `object`, which
/// does not call a `__getattribute__` or `__getattr__` of the type.
fn generic_getattr<'py>(
    item: &Bound<'py, PyAny>,
    name: &Bound<'py, PyString>,
) -> PyResult<Bound<'py, PyAny>> {
    // SAFETY: both arguments are live objects.
    unsafe {
        Bound::from_owned_ptr_or_err(
            item.py(),
            ffi::PyObject_GenericGetAttr(item.as_ptr(), name.as_ptr()),
        )
    }
}

/// Read the field `name` of an object with a `__dict__` that does not hold
/// it. msgspec reads such a field only from a slot, so the type must have a
/// member descriptor for it. Otherwise the result is an AttributeError, and
/// the field is skipped.
fn slot_value<'py>(
    item: &Bound<'py, PyAny>,
    name: &Bound<'py, PyString>,
) -> PyResult<Bound<'py, PyAny>> {
    let py = item.py();
    match type_lookup(&item.get_type(), name)? {
        Some(descriptor)
            if descriptor.get_type().as_type_ptr() == &raw mut ffi::PyMemberDescr_Type =>
        {
            descriptor.call_method1(pyo3::intern!(py, "__get__"), (item, item.get_type()))
        }
        _ => Err(PyAttributeError::new_err(name.clone().unbind())),
    }
}

/// The maximum number of types in an `offsets` cache, as for the kinds.
#[cfg(not(Py_GIL_DISABLED))]
const MAX_CACHED_TYPES: usize = 4096;

/// Read the slot offset of each field of the type of `item` from its member
/// descriptor. Return None if a field is not a plain object slot. The caller
/// checks that the Python version has the `MemberDescr` layout.
///
/// Each offset must give the object that the lookup of `object` gives on
/// `item`. With `generic_only`, the type must also use that lookup itself.
#[cfg(not(Py_GIL_DISABLED))]
fn read_field_offsets(
    item: &Bound<'_, PyAny>,
    names: &Bound<'_, PyTuple>,
    generic_only: bool,
) -> PyResult<Option<Vec<ffi::Py_ssize_t>>> {
    let py = item.py();
    let item_type = item.get_type();
    if generic_only && !uses_generic_getattr(item_type.as_type_ptr()) {
        return Ok(None);
    }
    // The getset of `type` reads the real size: a metaclass can override
    // `__basicsize__`.
    static BASICSIZE: pyo3::sync::PyOnceLock<Py<PyAny>> = pyo3::sync::PyOnceLock::new();
    let basicsize: ffi::Py_ssize_t = type_getset(py, &BASICSIZE, "__basicsize__")?
        .call_method1(pyo3::intern!(py, "__get__"), (&item_type,))?
        .extract()?;
    let pointer_size = std::mem::size_of::<*mut ffi::PyObject>() as ffi::Py_ssize_t;
    let header_size = std::mem::size_of::<ffi::PyObject>() as ffi::Py_ssize_t;
    let mut offsets = Vec::with_capacity(names.len());
    for name in names.iter() {
        let name = name.cast::<PyString>()?;
        // The lookup in the dicts of the MRO, not `getattr`: a metaclass can
        // give any object for a name.
        let Some(descriptor) = type_lookup(&item_type, name)? else {
            return Ok(None);
        };
        if descriptor.get_type().as_type_ptr() != &raw mut ffi::PyMemberDescr_Type {
            return Ok(None);
        }
        // SAFETY: the object is a member descriptor, and `MemberDescr` is its
        // layout on the Python versions that the caller checked.
        let (member, owner) = unsafe {
            let descriptor = descriptor.as_ptr().cast::<MemberDescr>();
            ((*descriptor).d_member, (*descriptor).d_type)
        };
        // The descriptor must belong to a base of the type, so its offset is
        // inside the layout of the instance.
        // SAFETY: both are live types, and the check runs no Python code.
        if member.is_null()
            || owner.is_null()
            || unsafe { ffi::PyType_IsSubtype(item_type.as_type_ptr(), owner) } == 0
        {
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
        let same = match generic_getattr(item, name) {
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
/// - a tuple of field names: the scan reads these fields from their slots,
///   as msgspec does for a Struct.
/// - an instance of a tuple subclass: the scan reads these attributes as
///   msgspec does for a dataclass. It reads a slot in place only if the type
///   has no attribute lookup of its own.
/// - any other value (Python gives `True`): the object is a Serializer.
///
/// `classify(type)` gives the kind of a type that `kinds` does not hold.
/// `offsets`, if given, keeps the slot offsets of the fields of each type,
/// so the scan reads them without an attribute lookup.
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
        leaf_count: 0,
        next_leaf: 0,
        offsets,
        struct_type: std::ptr::null_mut(),
        struct_offsets: Vec::new(),
        #[cfg(not(Py_GIL_DISABLED))]
        layouts: layouts_known(value.py()),
        #[cfg(Py_GIL_DISABLED)]
        layouts: false,
        dedup: false,
        seen: AddressSet::default(),
        seen_refs: Vec::new(),
    };
    if scan.visit(value, 1)? {
        return Ok(true);
    }
    while let Some((item, depth)) = scan.stack.pop() {
        if depth >= MAX_DEPTH {
            return Err(PyRecursionError::new_err(
                "maximum recursion depth exceeded while scanning the response for a Serializer",
            ));
        }
        if !scan.dedup && scan.stack.len() < DEDUP_STACK {
            if scan.visit(&item, depth + 1)? {
                return Ok(true);
            }
            continue;
        }
        scan.dedup = true;
        if !scan.seen.insert(item.as_ptr() as usize) {
            continue;
        }
        if scan.visit(&item, depth + 1)? {
            return Ok(true);
        }
        scan.seen_refs.push(item);
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

class Attributes(tuple):
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
                // A dict of 8 or more values: the scan reads a copy of its values.
                "{**{str(i): i for i in range(20)}, 'z': Secret()}",
                "collections.OrderedDict({**{str(i): [i] for i in range(9)}, 'z': (Secret(),)})",
                "[*range(20), Secret()]",
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
                "{str(i): [i, {'k': Empty()}] for i in range(20)}",
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

    #[test]
    fn a_dict_or_a_set_that_changes_during_a_kind_lookup_does_not_crash_the_scan() {
        Python::initialize();
        Python::attach(|py| {
            let module = fixtures(py);
            let code = c"
class Clearing(type):
    def __hash__(cls):
        small.clear()
        large.clear()
        members.clear()
        return type.__hash__(cls)

class Item(metaclass=Clearing):
    pass

small = {'a': Item(), 'b': Item(), 'c': [Secret()]}
large = {**{str(i): Item() for i in range(10)}, 'z': [Secret()]}
members = {Item(), Item(), Item()}
";
            py.run(code, Some(&module.dict()), None).unwrap();
            for name in ["small", "large"] {
                let value = eval(&module, name);
                scan(&module, &value).unwrap();
                assert_eq!(value.len().unwrap(), 0, "{name}");
            }
            // Python raises an error for a set that changes during a loop.
            py.run(
                c"members = {Item(), Item(), Item()}",
                Some(&module.dict()),
                None,
            )
            .unwrap();
            let members = eval(&module, "members");
            let err = scan(&module, &members).unwrap_err();
            assert!(err.is_instance_of::<pyo3::exceptions::PyRuntimeError>(py));
        });
    }

    #[test]
    fn a_struct_kind_reads_slots_and_an_attribute_kind_reads_with_getattr() {
        Python::initialize();
        Python::attach(|py| {
            let module = fixtures(py);
            // msgspec reads the fields of a Struct from their slots. It reads
            // the fields of a dataclass with getattr if the class has its own
            // lookup. A tuple kind stands for a Struct, a tuple subclass for a
            // dataclass.
            let code = c"
class Masked:
    __slots__ = ('inner',)
    def __init__(self, inner):
        self.inner = inner
    def __getattribute__(self, name):
        return 1 if name == 'inner' else object.__getattribute__(self, name)

class MaskedFields(Masked):
    __slots__ = ()

class Lazy:
    __slots__ = ('inner',)
    def __getattr__(self, name):
        return Secret()

class Plain:
    __slots__ = ('inner',)
    def __init__(self, inner):
        self.inner = inner

KINDS[Masked] = ('inner',)
KINDS[MaskedFields] = Attributes(('inner',))
KINDS[Lazy] = Attributes(('inner',))
KINDS[Plain] = Attributes(('inner',))
";
            py.run(code, Some(&module.dict()), None).unwrap();
            for offsets in [None, Some(PyDict::new(py))] {
                let run = |expr: &str| {
                    let value = eval(&module, expr);
                    match &offsets {
                        Some(offsets) => scan_with_offsets(&module, &value, offsets).unwrap(),
                        None => scan(&module, &value).unwrap(),
                    }
                };
                assert!(run("[Masked(1), Masked(Secret())]"));
                assert!(!run("[MaskedFields(1), MaskedFields(Secret())]"));
                assert!(run("[Lazy(), Lazy()]"));
                assert!(!run("[Plain(1), Plain(Empty())]"));
                assert!(run("[Plain(1), Plain([Secret()])]"));
            }
            // A dataclass with its own lookup keeps getattr, a plain one reads slots.
            #[cfg(not(Py_GIL_DISABLED))]
            {
                let offsets = PyDict::new(py);
                for expr in ["[Lazy()]", "[Plain(1)]"] {
                    scan_with_offsets(&module, &eval(&module, expr), &offsets).unwrap();
                }
                let lazy = offsets.get_item(eval(&module, "Lazy")).unwrap().unwrap();
                assert!(lazy.is_none());
                let plain = offsets.get_item(eval(&module, "Plain")).unwrap().unwrap();
                assert!(plain.cast::<FieldOffsets>().is_ok());
            }
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
    fn a_type_equal_to_another_type_does_not_take_its_offsets() {
        Python::initialize();
        Python::attach(|py| {
            let module = fixtures(py);
            // The offsets dict compares types with __eq__. These two types are
            // equal, and the small one must not be read with the offsets of the
            // big one.
            let code = c"
class EqualByName(type):
    def __eq__(cls, other):
        return isinstance(other, type) and cls.__name__ == other.__name__
    def __hash__(cls):
        return hash(cls.__name__)

def make(count):
    names = tuple(f'f{index}' for index in range(count))
    def init(self, *values):
        for name, value in zip(names, values):
            setattr(self, name, value)
    return EqualByName('Row', (), {'__slots__': names, '__init__': init})

Small = make(1)
Big = make(16)
KINDS[Small] = ('f0',)
KINDS[Big] = tuple(f'f{index}' for index in range(16))
";
            py.run(code, Some(&module.dict()), None).unwrap();
            let offsets = PyDict::new(py);
            let big = eval(&module, "[Big(*range(16))]");
            assert!(!scan_with_offsets(&module, &big, &offsets).unwrap());
            let small = eval(
                &module,
                "[Small(1) for _ in range(1000)] + [Small(Secret())]",
            );
            assert!(scan_with_offsets(&module, &small, &offsets).unwrap());
            let plain = eval(&module, "[Small(1) for _ in range(1000)]");
            assert!(!scan_with_offsets(&module, &plain, &offsets).unwrap());
        });
    }

    #[test]
    fn a_type_that_cannot_be_a_dict_key_gets_its_kind_each_time() {
        Python::initialize();
        Python::attach(|py| {
            let module = fixtures(py);
            let code = c"
class IdentityEq(type):
    def __eq__(cls, other):
        return cls is other
    __hash__ = None

class Box(metaclass=IdentityEq):
    __slots__ = ('inner',)
    def __init__(self, inner):
        self.inner = inner

def classify_box(tp):
    calls.append(tp)
    return ('inner',) if tp is Box else classify(tp)
";
            py.run(code, Some(&module.dict()), None).unwrap();
            let kinds = module
                .getattr("kinds")
                .unwrap()
                .call0()
                .unwrap()
                .cast_into::<PyDict>()
                .unwrap();
            let classify = module.getattr("classify_box").unwrap();
            let offsets = PyDict::new(py);
            for (expr, found) in [
                ("[Box(1), Box(2)]", false),
                ("[Box(1), Box(Secret())]", true),
            ] {
                let value = eval(&module, expr);
                assert_eq!(
                    find_serializer(&value, &kinds, &classify, Some(&offsets)).unwrap(),
                    found,
                    "{expr}"
                );
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
    fn a_value_deeper_than_the_limit_raises_recursion_error() {
        Python::initialize();
        Python::attach(|py| {
            let module = fixtures(py);
            let mut deep = PyList::empty(py).into_any();
            for _ in 0..MAX_DEPTH {
                deep = PyList::new(py, [deep]).unwrap().into_any();
            }
            let is_recursion_error = |err: PyErr| err.is_instance_of::<PyRecursionError>(py);
            assert!(is_recursion_error(scan(&module, &deep).unwrap_err()));
            // A cycle with a small fan-out reaches the depth limit.
            let cyclic = eval(&module, "(lambda a: (a.append(a), a)[1])([])");
            assert!(is_recursion_error(scan(&module, &cyclic).unwrap_err()));
            let cyclic_with_secret = eval(
                &module,
                "(lambda a: (a.append(a), a.append(Secret()), a)[2])([])",
            );
            assert!(scan(&module, &cyclic_with_secret).unwrap());
            // A cycle with a large fan-out fills the stack first. From
            // DEDUP_STACK the scan visits each object once, so it ends.
            let code = c"
parent = {'children': []}
parent['children'].extend({'id': index, 'parent': parent} for index in range(2000))
parent_with_secret = {'children': [], 'secret': [Secret()]}
parent_with_secret['children'].extend({'id': index, 'parent': parent_with_secret} for index in range(2000))
";
            py.run(code, Some(&module.dict()), None).unwrap();
            assert!(!scan(&module, &eval(&module, "parent")).unwrap());
            assert!(scan(&module, &eval(&module, "parent_with_secret")).unwrap());

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
                prop::collection::vec(inner.clone(), 0..12).prop_map(Tree::List),
                prop::collection::vec(inner.clone(), 0..5).prop_map(Tree::Tuple),
                // From 8 values, the scan reads a copy of the values of a dict.
                prop::collection::btree_map("[a-z]{1,3}", inner.clone(), 0..12)
                    .prop_map(Tree::Dict),
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
