//! Find a Bolt Serializer in a response value before msgspec encodes it.
//!
//! msgspec encodes each Struct natively, so a Serializer that a container
//! holds would send its write-only fields. `SerializerScanEncoder.encode`
//! scans the value first. It calls the Python dump only when the scan finds a
//! Serializer, so a value with no Serializer costs one native pass.

use pyo3::exceptions::{PyAttributeError, PyRecursionError, PyTypeError};
use pyo3::ffi;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyIterator, PyList, PyString, PyTuple, PyType};

/// The deepest level that the scan visits. At this level the scan raises
/// RecursionError, as msgspec does for such a value or for a cycle. The scan
/// keeps its own stack, so a deep value cannot overflow the thread stack.
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
/// Struct. The tests of this module block msgspec, because plain classes
/// stand for Structs there.
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

/// Return the `__dict__` of `item`, or None if it has no dict. msgspec reads
/// a dataclass field from this dict first.
fn instance_dict<'py>(item: &Bound<'py, PyAny>) -> Option<Bound<'py, PyDict>> {
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

/// The number of types whose read plan one scan keeps. A value with more
/// types makes the plan of a type again after the type left the table.
const PLAN_TYPES: usize = 16;

/// The maximum number of types in an `offsets` cache. When the cache is
/// full, its oldest entry goes. Thus the cache keeps at most this many types
/// alive, also when an app makes types at runtime.
#[cfg(not(Py_GIL_DISABLED))]
const MAX_CACHED_TYPES: usize = 512;

/// The slot offsets of the fields of a Struct type, which `find_serializer`
/// keeps in its `offsets` dict. The scan reads each field at its offset with
/// no attribute lookup, as msgspec does.
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
/// own.
fn uses_generic_getattr(item_type: *mut ffi::PyTypeObject) -> bool {
    // SAFETY: `item_type` is a live type, and the slot read runs no Python code.
    let getattro = unsafe { ffi::PyType_GetSlot(item_type, ffi::Py_tp_getattro) };
    getattro == ffi::PyObject_GenericGetAttr as *mut std::ffi::c_void
}

/// Return whether an instance of `item_type` can have a `__dict__`. The
/// getset of `type` reads the real `tp_dictoffset`.
fn has_instance_dict(item_type: &Bound<'_, PyType>) -> PyResult<bool> {
    static DICTOFFSET: pyo3::sync::PyOnceLock<Py<PyAny>> = pyo3::sync::PyOnceLock::new();
    let py = item_type.py();
    let offset: ffi::Py_ssize_t = type_getset(py, &DICTOFFSET, "__dictoffset__")?
        .call_method1(pyo3::intern!(py, "__get__"), (item_type,))?
        .extract()?;
    Ok(offset != 0)
}

/// The real `tp_basicsize` of `item_type`, from the getset of `type`. A
/// metaclass can override `__basicsize__`.
#[cfg(not(Py_GIL_DISABLED))]
fn basic_size(item_type: &Bound<'_, PyType>) -> PyResult<ffi::Py_ssize_t> {
    static BASICSIZE: pyo3::sync::PyOnceLock<Py<PyAny>> = pyo3::sync::PyOnceLock::new();
    let py = item_type.py();
    type_getset(py, &BASICSIZE, "__basicsize__")?
        .call_method1(pyo3::intern!(py, "__get__"), (item_type,))?
        .extract()
}

/// Whether `descriptor` is a data descriptor: its type has both `__get__`
/// and `__set__`. msgspec reads only such a descriptor for a dataclass field
/// that the `__dict__` does not hold.
fn is_data_descriptor(descriptor: &Bound<'_, PyAny>) -> bool {
    !descriptor_slot(descriptor, ffi::Py_tp_descr_get).is_null()
        && !descriptor_slot(descriptor, ffi::Py_tp_descr_set).is_null()
}

/// Call the `__get__` of `descriptor` for `item`, as msgspec does. Return
/// None if the call fails: msgspec then skips the field.
fn descriptor_get<'py>(
    descriptor: &Bound<'py, PyAny>,
    item: &Bound<'py, PyAny>,
    item_type: &Bound<'py, PyType>,
) -> Option<Bound<'py, PyAny>> {
    let get = descriptor_slot(descriptor, ffi::Py_tp_descr_get);
    if get.is_null() {
        return None;
    }
    // SAFETY: `get` is the `tp_descr_get` slot of the type of `descriptor`,
    // and the three arguments are live objects.
    let value = unsafe {
        let get: ffi::descrgetfunc = std::mem::transmute(get);
        get(descriptor.as_ptr(), item.as_ptr(), item_type.as_ptr())
    };
    // SAFETY: the call gives a new reference or null with an exception set.
    // msgspec clears the exception and skips the field.
    unsafe { Bound::from_owned_ptr_or_err(item.py(), value) }.ok()
}

/// Whether `key` is a str that msgspec encodes as a key of a plain object:
/// an exact str that does not start with an underscore.
fn is_public_key(key: &Bound<'_, PyAny>) -> bool {
    if !key.is_exact_instance_of::<PyString>() {
        return false;
    }
    // SAFETY: `key` is an exact str, and the reads run no Python code.
    unsafe {
        ffi::PyUnicode_GetLength(key.as_ptr()) == 0
            || ffi::PyUnicode_ReadChar(key.as_ptr(), 0) != u32::from(b'_')
    }
}

/// How the scan reads the public object slots of an attrs type.
enum Slots<'py> {
    /// The offsets of the slots, from the member tables of the types in the
    /// base chain, as msgspec reads them.
    #[cfg(not(Py_GIL_DISABLED))]
    Offsets(Box<[ffi::Py_ssize_t]>),
    /// The member descriptors in the dicts of the types in the base chain,
    /// when the scan cannot read the member tables.
    Descriptors(Vec<Bound<'py, PyAny>>),
}

/// How the scan reads the fields of a type, as msgspec encodes them.
enum Plan<'py> {
    /// The fields are object slots at these offsets: a Struct, or a dataclass
    /// whose objects have no `__dict__` and whose fields are plain slots.
    #[cfg(not(Py_GIL_DISABLED))]
    Offsets(Box<[ffi::Py_ssize_t]>),
    /// A Struct whose fields the scan cannot read in place.
    Struct(Bound<'py, PyTuple>),
    /// A dataclass. msgspec reads each key of `__dataclass_fields__`.
    Dataclass {
        names: Bound<'py, PyTuple>,
        /// Whether the type uses the attribute lookup of `object`. If not,
        /// msgspec reads each field with `getattr`.
        generic: bool,
        has_dict: bool,
        /// For each name, the data descriptor that the type lookup finds.
        descriptors: Vec<Option<Bound<'py, PyAny>>>,
    },
    /// An attrs object, which msgspec encodes as a plain object: the public
    /// keys of its `__dict__`, then its public object slots.
    Object { has_dict: bool, slots: Slots<'py> },
}

/// A container or an object whose children the scan reads now.
enum Frame<'py> {
    /// A dict and the position of `PyDict_Next`.
    #[cfg(not(Py_GIL_DISABLED))]
    Dict(Bound<'py, PyAny>, ffi::Py_ssize_t),
    /// A list and the next index.
    List(Bound<'py, PyAny>, ffi::Py_ssize_t),
    /// A tuple and the next index.
    Tuple(Bound<'py, PyAny>, ffi::Py_ssize_t),
    /// An iterator over a set or a frozenset.
    Iter(Bound<'py, PyIterator>),
    /// The fields that the scan read from an object: the items of `values`
    /// from `start`, with the next one at `next`.
    Values { start: usize, next: usize },
}

/// The state of one scan. The scan goes depth first, so the frames hold one
/// container or object for each level, and a wide value costs no memory.
struct Scan<'a, 'py> {
    py: Python<'py>,
    frames: Vec<Frame<'py>>,
    /// The fields of the objects in `Values` frames. A frame owns the items
    /// from its `start` to the `start` of the next `Values` frame.
    values: Vec<Bound<'py, PyAny>>,
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
    /// The read plans of the last types with fields. The scan makes a plan
    /// once per type and scan: an app can change a type between two scans.
    plans: Vec<(Bound<'py, PyType>, Plan<'py>)>,
    next_plan: usize,
    /// The plans that the scan last read at offsets, for `Skipper`.
    #[cfg(not(Py_GIL_DISABLED))]
    hot_plans: [usize; HOT_TYPES],
    #[cfg(not(Py_GIL_DISABLED))]
    next_hot: usize,
    /// The cache of the field offsets of Struct types, keyed by type.
    #[cfg_attr(Py_GIL_DISABLED, allow(dead_code))]
    offsets: Option<&'a Bound<'py, PyDict>>,
    /// Whether the scan can read lists, member descriptors and member
    /// tables in place.
    #[cfg_attr(Py_GIL_DISABLED, allow(dead_code))]
    layouts: bool,
}

/// Whether the scan skips an object of type `child_type` with no reference
/// count change: an exact str, int, float or bool, None, or a leaf type.
///
/// # Safety
///
/// `child` points to a live object.
#[cfg(not(Py_GIL_DISABLED))]
#[inline(always)]
unsafe fn is_skipped(
    child: *mut ffi::PyObject,
    leaf_types: &[*mut ffi::PyTypeObject; LEAF_TYPES],
) -> bool {
    // SAFETY: the caller keeps `child` alive.
    let child_type = unsafe { ffi::Py_TYPE(child) };
    // The checks go in the order of how common the types are in JSON.
    child_type == &raw mut ffi::PyUnicode_Type
        || (!is_exact_container_type(child_type)
            && (is_exact_scalar_type(child_type)
                || child == unsafe { ffi::Py_None() }
                || leaf_types.contains(&child_type)))
}

/// The number of types that the container loops read at offsets in place.
#[cfg(not(Py_GIL_DISABLED))]
const HOT_TYPES: usize = 4;

/// What the container loops skip with no reference count change: leaves,
/// and an object of one of the last types that the scan read at offsets
/// when each of its fields is a leaf. A list of Structs with leaf fields,
/// also of a few types such as a tagged union, thus costs a few pointer
/// reads for each item.
#[cfg(not(Py_GIL_DISABLED))]
struct Skipper<'s> {
    leaf_types: &'s [*mut ffi::PyTypeObject; LEAF_TYPES],
    hot: [(*mut ffi::PyTypeObject, &'s [ffi::Py_ssize_t]); HOT_TYPES],
    /// The number of entries of `hot` in use, from the start.
    hot_count: usize,
}

#[cfg(not(Py_GIL_DISABLED))]
impl Skipper<'_> {
    /// Whether the loop skips `child`.
    ///
    /// # Safety
    ///
    /// `child` points to a live object, and the thread holds the GIL.
    #[inline(always)]
    unsafe fn skips(&self, child: *mut ffi::PyObject) -> bool {
        // SAFETY: the caller keeps `child` alive. The plan of `hot_type`
        // checked each offset against its layout. The reads run no Python code.
        unsafe {
            if is_skipped(child, self.leaf_types) {
                return true;
            }
            let child_type = ffi::Py_TYPE(child);
            let Some((_, offsets)) = self.hot[..self.hot_count]
                .iter()
                .find(|(hot_type, _)| *hot_type == child_type)
            else {
                return false;
            };
            offsets.iter().all(|offset| {
                let field = *child
                    .cast::<u8>()
                    .offset(*offset)
                    .cast::<*mut ffi::PyObject>();
                field.is_null() || is_skipped(field, self.leaf_types)
            })
        }
    }
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

    /// Put `frame` on the stack. At `MAX_DEPTH` frames, raise RecursionError,
    /// as msgspec does for such a value or for a cycle.
    fn push_frame(&mut self, frame: Frame<'py>) -> PyResult<()> {
        if self.frames.len() >= MAX_DEPTH {
            return Err(PyRecursionError::new_err(
                "maximum recursion depth exceeded while scanning the response for a Serializer",
            ));
        }
        self.frames.push(frame);
        Ok(())
    }

    /// Take the top frame off the stack, and drop the values that it owns.
    fn pop_frame(&mut self) {
        if let Some(Frame::Values { start, .. }) = self.frames.pop() {
            self.values.truncate(start);
        }
    }

    /// Return the next child of the top frame that the scan must look at, or
    /// None when no frame is left. A frame with no child left goes. Leaves
    /// are skipped here, with no reference count change on a GIL build.
    fn next_child(&mut self) -> PyResult<Option<Bound<'py, PyAny>>> {
        let py = self.py;
        loop {
            let leaf_types = &self.leaf_types;
            #[cfg(not(Py_GIL_DISABLED))]
            let skipper = {
                let mut hot = [(std::ptr::null_mut(), &[][..]); HOT_TYPES];
                let mut hot_count = 0;
                for index in self.hot_plans {
                    if let Some((hot_type, Plan::Offsets(offsets))) = self.plans.get(index) {
                        hot[hot_count] = (hot_type.as_type_ptr(), &offsets[..]);
                        hot_count += 1;
                    }
                }
                Skipper {
                    leaf_types,
                    hot,
                    hot_count,
                }
            };
            #[cfg(Py_GIL_DISABLED)]
            let skipper = *leaf_types;
            let Some(frame) = self.frames.last_mut() else {
                return Ok(None);
            };
            let child = match frame {
                #[cfg(not(Py_GIL_DISABLED))]
                Frame::Dict(dict, position) => {
                    let mut key = std::ptr::null_mut();
                    let mut value = std::ptr::null_mut();
                    let mut found = None;
                    // SAFETY: the frame holds the dict, and the thread holds
                    // the GIL. A kind lookup can run Python code that changes
                    // the dict between two calls. PyDict_Next checks its
                    // position against the current table, so such a change
                    // cannot crash it.
                    unsafe {
                        while ffi::PyDict_Next(dict.as_ptr(), position, &mut key, &mut value) != 0 {
                            if !skipper.skips(value) {
                                found = Some(Bound::from_borrowed_ptr(py, value));
                                break;
                            }
                        }
                    }
                    found
                }
                Frame::List(list, index) => next_list_item(py, list, index, &skipper, self.layouts),
                Frame::Tuple(tuple, index) => next_tuple_item(py, tuple, index, &skipper),
                Frame::Iter(iter) => match iter.next() {
                    Some(child) => Some(child?),
                    None => None,
                },
                Frame::Values { next, .. } => {
                    if *next < self.values.len() {
                        *next += 1;
                        Some(self.values[*next - 1].clone())
                    } else {
                        None
                    }
                }
            };
            match child {
                Some(child) => return Ok(Some(child)),
                None => self.pop_frame(),
            }
        }
    }

    /// Look at `item`: skip it, put a frame for it on the stack, or return
    /// true if it is a Serializer. The scan holds a strong reference to
    /// `item`, so the Python code of a kind lookup cannot free it.
    fn enter(&mut self, item: Bound<'py, PyAny>) -> PyResult<bool> {
        if is_exact_leaf(&item) {
            return Ok(false);
        }
        // SAFETY: `item` is alive, so its type is alive. The read runs no Python code.
        let type_ptr = unsafe { ffi::Py_TYPE(item.as_ptr()) };
        if is_exact_container_type(type_ptr) {
            self.push_container(item)?;
            return Ok(false);
        }
        if let Some(index) = self
            .plans
            .iter()
            .position(|(seen, _)| seen.as_type_ptr() == type_ptr)
        {
            return self.read_fields(&item, index);
        }
        if self.is_leaf_type(type_ptr) {
            return Ok(false);
        }
        let item_type = item.get_type();
        let kind = match self.kinds.get_item(&item_type) {
            Ok(Some(kind)) => kind,
            Ok(None) => {
                // `classify` never gets a container or a scalar subclass, so
                // only a type that `kinds` does not hold needs these checks.
                if self.enter_by_flags(&item, &item_type)? {
                    return Ok(false);
                }
                self.classify.call1((&item_type,))?
            }
            // A type whose metaclass has __eq__ and no __hash__ cannot be a
            // key, so it gets its kind each time.
            Err(err) if err.is_instance_of::<PyTypeError>(self.py) => {
                if self.enter_by_flags(&item, &item_type)? {
                    return Ok(false);
                }
                self.classify.call1((&item_type,))?
            }
            Err(err) => return Err(err),
        };
        if kind.is_none() {
            self.add_leaf_type(type_ptr);
            return Ok(false);
        }
        let Ok(names) = kind.cast_into::<PyTuple>() else {
            // A Serializer.
            return Ok(true);
        };
        let plan = self.make_plan(&item, &item_type, names)?;
        let index = if self.plans.len() < PLAN_TYPES {
            self.plans.push((item_type.clone(), plan));
            self.plans.len() - 1
        } else {
            let index = self.next_plan;
            self.plans[index] = (item_type.clone(), plan);
            self.next_plan = (index + 1) % PLAN_TYPES;
            index
        };
        self.read_fields(&item, index)
    }

    /// Handle a container or a scalar subclass. Return false for any other
    /// type: it gets its kind.
    fn enter_by_flags(
        &mut self,
        item: &Bound<'py, PyAny>,
        item_type: &Bound<'py, PyType>,
    ) -> PyResult<bool> {
        let type_ptr = item_type.as_type_ptr();
        // SAFETY: `item_type` is the type of a live object, so it is alive.
        // The flag reads and the subtype checks run no Python code.
        let (container, set, scalar) = unsafe {
            let flags = ffi::PyType_GetFlags(type_ptr);
            let is_subtype =
                |base: *mut ffi::PyTypeObject| ffi::PyType_IsSubtype(type_ptr, base) != 0;
            (
                flags
                    & (ffi::Py_TPFLAGS_DICT_SUBCLASS
                        | ffi::Py_TPFLAGS_LIST_SUBCLASS
                        | ffi::Py_TPFLAGS_TUPLE_SUBCLASS)
                    != 0,
                is_subtype(&raw mut ffi::PySet_Type) || is_subtype(&raw mut ffi::PyFrozenSet_Type),
                flags
                    & (ffi::Py_TPFLAGS_UNICODE_SUBCLASS
                        | ffi::Py_TPFLAGS_LONG_SUBCLASS
                        | ffi::Py_TPFLAGS_BYTES_SUBCLASS)
                    != 0
                    || is_subtype(&raw mut ffi::PyFloat_Type)
                    || is_subtype(&raw mut ffi::PyByteArray_Type),
            )
        };
        if container {
            // msgspec reads the storage of a subclass too, not its __iter__.
            self.push_container(item.clone())?;
            return Ok(true);
        }
        if set {
            // msgspec iterates a set. The iterator raises RuntimeError when
            // the set changes during the loop.
            let iter = item.try_iter()?;
            self.push_frame(Frame::Iter(iter))?;
            return Ok(true);
        }
        // msgspec encodes the fields of a dataclass or an attrs class, also of
        // a subclass of a scalar type. Such a type gets its kind.
        if scalar && !has_fields(item_type.as_any())? {
            self.add_leaf_type(type_ptr);
            return Ok(true);
        }
        Ok(false)
    }

    /// Put a frame for a dict, list or tuple (also a subclass) on the stack.
    fn push_container(&mut self, item: Bound<'py, PyAny>) -> PyResult<()> {
        let container = item.as_ptr();
        // SAFETY: `item` is a live object, and the checks run no Python code.
        let (dict, list) = unsafe {
            (
                ffi::PyDict_Check(container) != 0,
                ffi::PyList_Check(container) != 0,
            )
        };
        let frame = if dict {
            #[cfg(not(Py_GIL_DISABLED))]
            {
                Frame::Dict(item, 0)
            }
            // Another thread can change the dict. A copy of its values does not change.
            #[cfg(Py_GIL_DISABLED)]
            {
                // SAFETY: `item` is a live dict. The call gives a new reference or null.
                let values = unsafe {
                    Bound::from_owned_ptr_or_err(self.py, ffi::PyDict_Values(container))?
                };
                Frame::List(values, 0)
            }
        } else if list {
            Frame::List(item, 0)
        } else {
            Frame::Tuple(item, 0)
        };
        self.push_frame(frame)
    }

    /// Make the read plan of `item_type` for the kind `names`.
    fn make_plan(
        &self,
        item: &Bound<'py, PyAny>,
        item_type: &Bound<'py, PyType>,
        names: Bound<'py, PyTuple>,
    ) -> PyResult<Plan<'py>> {
        let py = self.py;
        // Only the reads in place need the object itself.
        #[cfg(Py_GIL_DISABLED)]
        let _ = item;
        if names.is_exact_instance_of::<PyTuple>() {
            #[cfg(not(Py_GIL_DISABLED))]
            if let Some(offsets) = self.struct_offsets(item, item_type, &names)? {
                return Ok(Plan::Offsets(offsets));
            }
            return Ok(Plan::Struct(names));
        }
        let has_dict = has_instance_dict(item_type)?;
        if names.hasattr(pyo3::intern!(py, "object_fields"))? {
            return Ok(Plan::Object {
                has_dict,
                slots: self.object_slots(item_type)?,
            });
        }
        let generic = uses_generic_getattr(item_type.as_type_ptr());
        if !generic {
            return Ok(Plan::Dataclass {
                names,
                generic,
                has_dict,
                descriptors: Vec::new(),
            });
        }
        // With no `__dict__`, msgspec reads each field through the descriptor
        // of the type: for a plain slot, the scan reads the slot in place.
        #[cfg(not(Py_GIL_DISABLED))]
        if !has_dict
            && self.layouts
            && let Some(offsets) = read_field_offsets(item, &names, true)?
        {
            return Ok(Plan::Offsets(offsets.into_boxed_slice()));
        }
        let mut descriptors = Vec::with_capacity(names.len());
        for name in names.iter() {
            let descriptor = type_lookup(item_type, name.cast::<PyString>()?)?;
            descriptors.push(descriptor.filter(is_data_descriptor));
        }
        Ok(Plan::Dataclass {
            names,
            generic,
            has_dict,
            descriptors,
        })
    }

    /// Return the cached slot offsets of a Struct type, or read them from the
    /// first instance and cache them. Return None if a field is not a plain
    /// slot. The cache drops its oldest entry when it is full.
    #[cfg(not(Py_GIL_DISABLED))]
    fn struct_offsets(
        &self,
        item: &Bound<'py, PyAny>,
        item_type: &Bound<'py, PyType>,
        names: &Bound<'py, PyTuple>,
    ) -> PyResult<Option<Box<[ffi::Py_ssize_t]>>> {
        let Some(cache) = self.offsets else {
            return Ok(None);
        };
        let type_ptr = item_type.as_type_ptr();
        match cache.get_item(item_type) {
            Ok(Some(cached)) => {
                return Ok(cached
                    .cast::<FieldOffsets>()
                    .ok()
                    .and_then(|cached| cached.get().for_type(type_ptr).map(Box::from)));
            }
            Ok(None) => {}
            // A type whose metaclass has __eq__ and no __hash__ cannot be a key.
            Err(err) if err.is_instance_of::<PyTypeError>(self.py) => return Ok(None),
            Err(err) => return Err(err),
        }
        let offsets = if self.layouts {
            read_field_offsets(item, names, false)?
        } else {
            None
        };
        if cache.len() >= MAX_CACHED_TYPES {
            // A dict keeps the order of insertion, so its first key is the oldest.
            let oldest = cache.keys().get_item(0)?;
            cache.del_item(oldest)?;
        }
        match &offsets {
            Some(offsets) => {
                let cached = FieldOffsets {
                    offsets: offsets.clone().into_boxed_slice(),
                    owner: item_type.clone().into_any().unbind(),
                };
                cache.set_item(item_type, Py::new(self.py, cached)?)?;
            }
            None => cache.set_item(item_type, self.py.None())?,
        }
        Ok(offsets.map(Vec::into_boxed_slice))
    }

    /// Return how the scan reads the public object slots of `item_type`.
    fn object_slots(&self, item_type: &Bound<'py, PyType>) -> PyResult<Slots<'py>> {
        #[cfg(not(Py_GIL_DISABLED))]
        if self.layouts
            && let Some(offsets) = member_table_offsets(item_type)?
        {
            return Ok(Slots::Offsets(offsets));
        }
        Ok(Slots::Descriptors(member_descriptors(item_type)?))
    }

    /// Put the fields of `item` that plan `index` reads into a `Values`
    /// frame. A field that is a leaf does not go into the frame.
    fn read_fields(&mut self, item: &Bound<'py, PyAny>, index: usize) -> PyResult<bool> {
        let py = self.py;
        #[cfg(not(Py_GIL_DISABLED))]
        if matches!(self.plans[index].1, Plan::Offsets(_)) && !self.hot_plans.contains(&index) {
            self.hot_plans[self.next_hot] = index;
            self.next_hot = (self.next_hot + 1) % HOT_TYPES;
        }
        let start = self.values.len();
        let Scan {
            plans,
            values,
            leaf_types,
            ..
        } = self;
        let push = |values: &mut Vec<Bound<'py, PyAny>>, child: Bound<'py, PyAny>| {
            if !is_exact_leaf(&child) && !leaf_types.contains(&child.get_type().as_type_ptr()) {
                values.push(child);
            }
        };
        // A slot in place: a leaf costs no reference count change.
        #[cfg(not(Py_GIL_DISABLED))]
        let push_slot = |values: &mut Vec<Bound<'py, PyAny>>, offset: ffi::Py_ssize_t| {
            // SAFETY: the plan checked `offset` against the layout of the type
            // of `item`, which is alive. The checks before the new reference
            // run no Python code, and a set slot holds a strong reference.
            unsafe {
                let child = *item
                    .as_ptr()
                    .cast::<u8>()
                    .offset(offset)
                    .cast::<*mut ffi::PyObject>();
                if !child.is_null() && !is_skipped(child, leaf_types) {
                    values.push(Bound::from_borrowed_ptr(py, child));
                }
            }
        };
        match &plans[index].1 {
            #[cfg(not(Py_GIL_DISABLED))]
            Plan::Offsets(offsets) => {
                // `make_plan` checked each offset against the layout of the
                // type and against an instance. The plan belongs to the type
                // of `item`.
                for offset in offsets.iter() {
                    push_slot(values, *offset);
                }
            }
            Plan::Struct(names) => {
                if let Some(astuple) = astuple(py) {
                    // msgspec reads a Struct field from the slot that the class
                    // had when it was made, also when a property now hides it.
                    for child in astuple.call1((item,))?.cast::<PyTuple>()?.iter() {
                        push(values, child);
                    }
                } else {
                    for name in names.iter() {
                        match generic_getattr(item, name.cast::<PyString>()?) {
                            Ok(child) => push(values, child),
                            Err(err) if err.is_instance_of::<PyAttributeError>(py) => {}
                            Err(err) => return Err(err),
                        }
                    }
                }
            }
            Plan::Dataclass {
                names,
                generic,
                has_dict,
                descriptors,
            } => {
                if !*generic {
                    // msgspec uses the lookup of the type, and it skips a field
                    // whose lookup fails.
                    for name in names.iter() {
                        if let Ok(child) = item.getattr(name.cast::<PyString>()?) {
                            push(values, child);
                        }
                    }
                } else {
                    let dict = if *has_dict { instance_dict(item) } else { None };
                    let mut item_type = None;
                    for (index, descriptor) in descriptors.iter().enumerate() {
                        if let Some(dict) = &dict {
                            // With the GIL, a leaf costs no reference count change.
                            #[cfg(not(Py_GIL_DISABLED))]
                            // SAFETY: `names` is a live tuple with an item for
                            // each descriptor, and `dict` is a live dict. The
                            // checks before the new reference run no Python code.
                            unsafe {
                                let name =
                                    ffi::PyTuple_GetItem(names.as_ptr(), index as ffi::Py_ssize_t);
                                let child = ffi::PyDict_GetItemWithError(dict.as_ptr(), name);
                                if !child.is_null() {
                                    if !is_skipped(child, leaf_types) {
                                        values.push(Bound::from_borrowed_ptr(py, child));
                                    }
                                    continue;
                                }
                                if !ffi::PyErr_Occurred().is_null() {
                                    return Err(PyErr::fetch(py));
                                }
                            }
                            // Another thread can change the dict: `get_item` gives a strong reference.
                            #[cfg(Py_GIL_DISABLED)]
                            if let Some(child) = dict.get_item(names.get_item(index)?)? {
                                push(values, child);
                                continue;
                            }
                        }
                        if let Some(descriptor) = descriptor {
                            let item_type = item_type.get_or_insert_with(|| item.get_type());
                            if let Some(child) = descriptor_get(descriptor, item, item_type) {
                                push(values, child);
                            }
                        }
                    }
                }
            }
            Plan::Object { has_dict, slots } => {
                if *has_dict && let Some(dict) = instance_dict(item) {
                    // Another thread can change the dict, so the scan reads a copy.
                    #[cfg(Py_GIL_DISABLED)]
                    let dict = dict.copy()?;
                    for (key, child) in dict.iter() {
                        if is_public_key(&key) {
                            push(values, child);
                        }
                    }
                }
                match slots {
                    #[cfg(not(Py_GIL_DISABLED))]
                    Slots::Offsets(offsets) => {
                        // `member_table_offsets` read each offset from the
                        // member table of a base of the type of `item`, and
                        // checked it against its size.
                        for offset in offsets.iter() {
                            push_slot(values, *offset);
                        }
                    }
                    Slots::Descriptors(descriptors) => {
                        let item_type = item.get_type();
                        for descriptor in descriptors {
                            if let Some(child) = descriptor_get(descriptor, item, &item_type) {
                                push(values, child);
                            }
                        }
                    }
                }
            }
        }
        if self.values.len() > start {
            self.push_frame(Frame::Values { start, next: start })?;
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
        }
    }
}

/// Return the next item of `list` from `index` that the scan must look at.
///
/// A kind lookup can run Python code that changes the list between two
/// calls. Thus the loop reads the size and the item array again for each
/// index.
fn next_list_item<'py>(
    py: Python<'py>,
    list: &Bound<'py, PyAny>,
    index: &mut ffi::Py_ssize_t,
    #[cfg(not(Py_GIL_DISABLED))] skipper: &Skipper<'_>,
    #[cfg(Py_GIL_DISABLED)] leaf_types: &[*mut ffi::PyTypeObject; LEAF_TYPES],
    layouts: bool,
) -> Option<Bound<'py, PyAny>> {
    #[cfg(not(Py_GIL_DISABLED))]
    // SAFETY: the frame holds the list, and the thread holds the GIL. The
    // checks before the new reference run no Python code.
    unsafe {
        let pointer = list.as_ptr();
        if layouts {
            let layout = pointer.cast::<ListLayout>();
            while *index < (*layout).ob_size {
                let child = *(*layout).ob_item.offset(*index);
                *index += 1;
                if !skipper.skips(child) {
                    return Some(Bound::from_borrowed_ptr(py, child));
                }
            }
            return None;
        }
        while *index < ffi::PyList_Size(pointer) {
            let child = ffi::PyList_GetItem(pointer, *index);
            if child.is_null() {
                ffi::PyErr_Clear();
                return None;
            }
            *index += 1;
            if !skipper.skips(child) {
                return Some(Bound::from_borrowed_ptr(py, child));
            }
        }
        None
    }
    #[cfg(Py_GIL_DISABLED)]
    {
        let _ = (py, layouts);
        // Another thread can change the list. `get_item` gives a strong
        // reference, and it fails past the end.
        let list = list.cast::<PyList>().ok()?;
        loop {
            let child = list.get_item(usize::try_from(*index).ok()?).ok()?;
            *index += 1;
            if !is_exact_leaf(&child) && !leaf_types.contains(&child.get_type().as_type_ptr()) {
                return Some(child);
            }
        }
    }
}

/// Return the next item of `tuple` from `index` that the scan must look at.
/// A tuple does not change, and it holds its items.
fn next_tuple_item<'py>(
    py: Python<'py>,
    tuple: &Bound<'py, PyAny>,
    index: &mut ffi::Py_ssize_t,
    #[cfg(not(Py_GIL_DISABLED))] skipper: &Skipper<'_>,
    #[cfg(Py_GIL_DISABLED)] leaf_types: &[*mut ffi::PyTypeObject; LEAF_TYPES],
) -> Option<Bound<'py, PyAny>> {
    // SAFETY: the frame holds the tuple. The checks before the new reference
    // run no Python code, and the tuple keeps its items alive.
    unsafe {
        let pointer = tuple.as_ptr();
        while *index < ffi::PyTuple_Size(pointer) {
            let child = ffi::PyTuple_GetItem(pointer, *index);
            if child.is_null() {
                ffi::PyErr_Clear();
                return None;
            }
            *index += 1;
            #[cfg(not(Py_GIL_DISABLED))]
            if skipper.skips(child) {
                continue;
            }
            #[cfg(Py_GIL_DISABLED)]
            if leaf_types.contains(&ffi::Py_TYPE(child)) {
                continue;
            }
            let child = Bound::from_borrowed_ptr(py, child);
            if !is_exact_leaf(&child) {
                return Some(child);
            }
        }
        None
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

/// The slot `slot` of the type of `attribute`, for example `tp_descr_set`.
fn descriptor_slot(attribute: &Bound<'_, PyAny>, slot: std::ffi::c_int) -> *mut std::ffi::c_void {
    // SAFETY: the type of a live object is alive, and the slot read runs no Python code.
    unsafe { ffi::PyType_GetSlot(attribute.get_type().as_type_ptr(), slot) }
}

/// Return the base of `item_type` (`tp_base`), or None for `object`.
fn base_type<'py>(item_type: &Bound<'py, PyType>) -> Option<Bound<'py, PyType>> {
    // SAFETY: `item_type` is a live type. The slot read runs no Python code,
    // and it gives a borrowed reference that the type keeps alive.
    unsafe {
        let base = ffi::PyType_GetSlot(item_type.as_type_ptr(), ffi::Py_tp_base);
        if base.is_null() {
            return None;
        }
        Bound::from_borrowed_ptr(item_type.py(), base.cast())
            .cast_into::<PyType>()
            .ok()
    }
}

/// The member descriptors of the public object slots of `item_type` and its
/// bases (`tp_base`), from the dicts of these types. The scan uses them
/// where it cannot read the member tables.
fn member_descriptors<'py>(item_type: &Bound<'py, PyType>) -> PyResult<Vec<Bound<'py, PyAny>>> {
    static DICT: pyo3::sync::PyOnceLock<Py<PyAny>> = pyo3::sync::PyOnceLock::new();
    let py = item_type.py();
    let get = pyo3::intern!(py, "__get__");
    let dict_getset = type_getset(py, &DICT, "__dict__")?;
    let mut descriptors = Vec::new();
    let mut current = Some(item_type.clone());
    while let Some(owner) = current {
        let namespace = dict_getset.call_method1(get, (&owner,))?;
        for value in namespace
            .call_method0(pyo3::intern!(py, "values"))?
            .try_iter()?
        {
            let value = value?;
            if value.get_type().as_type_ptr() != &raw mut ffi::PyMemberDescr_Type
                || !value.getattr(pyo3::intern!(py, "__objclass__"))?.is(&owner)
            {
                continue;
            }
            if is_public_key(&value.getattr(pyo3::intern!(py, "__name__"))?) {
                descriptors.push(value);
            }
        }
        current = base_type(&owner);
    }
    Ok(descriptors)
}

/// The offsets of the public object slots of `item_type` and its bases
/// (`tp_base`), from the member table of each heap type, as msgspec reads
/// them: a member of type `Py_T_OBJECT_EX` that is not read-only and whose
/// name does not start with an underscore.
#[cfg(not(Py_GIL_DISABLED))]
fn member_table_offsets(item_type: &Bound<'_, PyType>) -> PyResult<Option<Box<[ffi::Py_ssize_t]>>> {
    let pointer_size = std::mem::size_of::<*mut ffi::PyObject>() as ffi::Py_ssize_t;
    let header_size = std::mem::size_of::<ffi::PyObject>() as ffi::Py_ssize_t;
    let item_size = basic_size(item_type)?;
    let mut offsets = Vec::new();
    let mut current = Some(item_type.clone());
    while let Some(owner) = current {
        let owner_ptr = owner.as_type_ptr();
        // SAFETY: `owner` is a live type, and the flag read runs no Python code.
        let heap = unsafe { ffi::PyType_GetFlags(owner_ptr) } & ffi::Py_TPFLAGS_HEAPTYPE != 0;
        if heap {
            let owner_size = basic_size(&owner)?;
            // The member table of a heap type follows the type object, at the
            // size of its metaclass. `ob_size` counts its members.
            let table_offset = basic_size(&owner.get_type())?;
            // SAFETY: a type object starts with the layout of a PyVarObject.
            let count = unsafe { (*owner_ptr.cast::<ffi::PyVarObject>()).ob_size };
            if !(0..=4096).contains(&count) || owner_size > item_size {
                // Not a layout that the scan knows: it reads the descriptors.
                return Ok(None);
            }
            for index in 0..count {
                // SAFETY: the type object holds `count` members at
                // `table_offset`, and it stays alive during the reads.
                let (name, type_code, offset, flags) = unsafe {
                    let member = owner_ptr
                        .cast::<u8>()
                        .offset(table_offset)
                        .cast::<ffi::PyMemberDef>()
                        .offset(index);
                    (
                        (*member).name,
                        (*member).type_code,
                        (*member).offset,
                        (*member).flags,
                    )
                };
                if name.is_null()
                    // SAFETY: a member name is a C string.
                    || unsafe { *name } as u8 == b'_'
                    || type_code != ffi::Py_T_OBJECT_EX
                    || flags & (ffi::Py_READONLY | ffi::Py_RELATIVE_OFFSET) != 0
                    || offset < header_size
                    || offset % pointer_size != 0
                    || offset > owner_size - pointer_size
                {
                    continue;
                }
                offsets.push(offset);
            }
        }
        current = base_type(&owner);
    }
    Ok(Some(offsets.into_boxed_slice()))
}

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
    let basicsize = basic_size(&item_type)?;
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
            || offset > basicsize - pointer_size
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

/// Whether `value` is an exact dict, list or tuple whose items are all an
/// exact str, int, float or bool, or None. Such a value, the most common
/// small response, needs no scan state. The check stops at the first other
/// item, so it costs little for any other value.
#[cfg(not(Py_GIL_DISABLED))]
fn holds_only_scalars(value: &Bound<'_, PyAny>) -> bool {
    let pointer = value.as_ptr();
    // SAFETY: the thread holds the GIL, `value` is alive, and the loops read
    // borrowed items with no Python code.
    unsafe {
        let is_scalar = |child: *mut ffi::PyObject| {
            is_exact_scalar_type(ffi::Py_TYPE(child)) || child == ffi::Py_None()
        };
        let value_type = ffi::Py_TYPE(pointer);
        if value_type == &raw mut ffi::PyDict_Type {
            let mut position: ffi::Py_ssize_t = 0;
            let mut key = std::ptr::null_mut();
            let mut item = std::ptr::null_mut();
            while ffi::PyDict_Next(pointer, &mut position, &mut key, &mut item) != 0 {
                if !is_scalar(item) {
                    return false;
                }
            }
            return true;
        }
        if value_type == &raw mut ffi::PyList_Type {
            return (0..ffi::PyList_Size(pointer))
                .all(|index| is_scalar(ffi::PyList_GetItem(pointer, index)));
        }
        if value_type == &raw mut ffi::PyTuple_Type {
            return (0..ffi::PyTuple_Size(pointer))
                .all(|index| is_scalar(ffi::PyTuple_GetItem(pointer, index)));
        }
    }
    false
}

/// Return whether `value` holds an object whose kind marks a Serializer.
///
/// The scan visits the items of dicts, lists, tuples, sets and frozensets.
/// For each other object, `kinds` maps its type to a kind:
///
/// - `None`: the scan does not visit the object.
/// - a tuple of field names: the fields of a Struct, which the scan reads
///   from their slots, as msgspec does.
/// - an instance of a tuple subclass: the fields of a dataclass, which the
///   scan reads as msgspec reads them. A tuple subclass with an
///   `object_fields` attribute marks an attrs class: the scan reads the
///   public keys of its `__dict__` and its public object slots.
/// - any other value (Python gives `True`): the object is a Serializer.
///
/// `classify(type)` gives the kind of a type that `kinds` does not hold.
/// `offsets`, if given, keeps the slot offsets of the fields of each Struct
/// type, so the scan reads them without an attribute lookup.
///
/// The scan goes depth first. At `MAX_DEPTH` levels it raises
/// RecursionError, as msgspec does for such a value or for a cycle.
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
    #[cfg(not(Py_GIL_DISABLED))]
    if holds_only_scalars(value) {
        return Ok(false);
    }
    let mut scan = Scan {
        py: value.py(),
        frames: Vec::new(),
        values: Vec::new(),
        kinds,
        classify,
        leaf_types: [std::ptr::null_mut(); LEAF_TYPES],
        leaf_count: 0,
        next_leaf: 0,
        plans: Vec::new(),
        next_plan: 0,
        #[cfg(not(Py_GIL_DISABLED))]
        hot_plans: [usize::MAX; HOT_TYPES],
        #[cfg(not(Py_GIL_DISABLED))]
        next_hot: 0,
        offsets,
        #[cfg(not(Py_GIL_DISABLED))]
        layouts: layouts_known(value.py()),
        #[cfg(Py_GIL_DISABLED)]
        layouts: false,
    };
    if scan.enter(value.clone())? {
        return Ok(true);
    }
    while let Some(child) = scan.next_child()? {
        if scan.enter(child)? {
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
import sys

# Plain classes stand for Structs here, and msgspec.structs.astuple rejects
# them. Block it, so that the tests do not depend on what the interpreter can
# import: `uv run cargo test` finds msgspec, a plain `cargo test` does not.
sys.modules['msgspec.structs'] = None

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
            // The cache keeps the offsets of Struct types only. A dataclass
            // gets its plan in each scan: an app can change the class.
            #[cfg(not(Py_GIL_DISABLED))]
            {
                let offsets = PyDict::new(py);
                for expr in ["[Lazy()]", "[Plain(1)]", "[Masked(1)]"] {
                    scan_with_offsets(&module, &eval(&module, expr), &offsets).unwrap();
                }
                assert_eq!(offsets.len(), 1);
                let masked = offsets.get_item(eval(&module, "Masked")).unwrap().unwrap();
                assert!(masked.cast::<FieldOffsets>().is_ok());
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
    fn an_object_kind_reads_the_public_dict_keys_and_slots_as_msgspec_does() {
        Python::initialize();
        Python::attach(|py| {
            let module = fixtures(py);
            // msgspec encodes an attrs object as a plain object: each public
            // key of its __dict__, then each public slot of its base chain.
            let code = c"
class ObjectKind(tuple):
    object_fields = True

class SlotBase:
    __slots__ = ('extra', '_hidden')

class Obj(SlotBase):
    pass

class Gone(SlotBase):
    pass

KINDS[Obj] = ObjectKind()
KINDS[Gone] = ObjectKind()

def make(**fields):
    obj = Obj()
    for name, value in fields.items():
        if name in ('extra', '_hidden'):
            setattr(obj, name, value)
        else:
            obj.__dict__[name] = value
    return obj
";
            py.run(code, Some(&module.dict()), None).unwrap();
            for (expr, expected) in [
                ("[make(extra=Secret())]", true),
                ("[make(_hidden=Secret())]", false),
                ("[make(public=Secret())]", true),
                ("[make(_private=Secret())]", false),
                ("[make(**{'1': Secret()})]", true),
                (
                    "[(lambda o: (o.__dict__.__setitem__(1, Secret()), o)[1])(make())]",
                    false,
                ),
                (
                    "[(lambda o: (o.__dict__.__setitem__(Text('key'), Secret()), o)[1])(make())]",
                    false,
                ),
                ("[make(extra=1, public=[2, Empty()])]", false),
                ("[make()]", false),
            ] {
                assert_eq!(
                    scan(&module, &eval(&module, expr)).unwrap(),
                    expected,
                    "{expr}"
                );
            }
            // msgspec reads the member table of the type, not its dict. A slot
            // whose descriptor the class lost still goes out.
            #[cfg(not(Py_GIL_DISABLED))]
            {
                let code = c"
gone = Gone()
gone.extra = Secret()
del SlotBase.extra
";
                py.run(code, Some(&module.dict()), None).unwrap();
                assert!(scan(&module, &eval(&module, "[gone]")).unwrap());
            }
        });
    }

    #[test]
    fn a_dataclass_kind_reads_the_dict_first_and_skips_a_failing_getter() {
        Python::initialize();
        Python::attach(|py| {
            let module = fixtures(py);
            // msgspec reads the __dict__ first, then a data descriptor of the
            // class, and it skips a field whose getter raises.
            let code = c"
class Stash:
    def __set_name__(self, owner, name):
        self.name = name
    def __get__(self, obj, cls=None):
        return None if obj is None else 'summary'
    def __set__(self, obj, value):
        obj.__dict__[self.name] = value

class Raising:
    def __get__(self, obj, cls=None):
        raise LookupError('not loaded')
    def __set__(self, obj, value):
        pass

class GetOnly:
    def __get__(self, obj, cls=None):
        return Secret()

class Row:
    actor = Stash()
    price = Raising()
    plain = 'default'
    computed = GetOnly()

class OwnLookup:
    def __getattr__(self, name):
        raise LookupError(name)

KINDS[Row] = Attributes(('actor', 'price', 'plain', 'computed', 'missing'))
KINDS[OwnLookup] = Attributes(('inner',))

def row(**fields):
    item = Row()
    item.__dict__.update(fields)
    return item
";
            py.run(code, Some(&module.dict()), None).unwrap();
            for (expr, expected) in [
                ("[row(actor=Secret())]", true),
                ("[row(actor=1, price=Secret())]", true),
                ("[row()]", false),
                ("[row(plain=Secret())]", true),
                ("[OwnLookup()]", false),
            ] {
                assert_eq!(
                    scan(&module, &eval(&module, expr)).unwrap(),
                    expected,
                    "{expr}"
                );
            }
            // msgspec skips a plain class attribute that the object does not hold.
            py.run(c"Row.plain = Secret()", Some(&module.dict()), None)
                .unwrap();
            assert!(!scan(&module, &eval(&module, "[row()]")).unwrap());
        });
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
            // The scan goes depth first, as msgspec encodes. A cycle before a
            // Serializer thus raises, as msgspec raises for it.
            let cyclic_with_secret = eval(
                &module,
                "(lambda a: (a.append(a), a.append(Secret()), a)[2])([])",
            );
            assert!(is_recursion_error(
                scan(&module, &cyclic_with_secret).unwrap_err()
            ));
            let secret_before_cycle = eval(
                &module,
                "(lambda a: (a.append(Secret()), a.append(a), a)[2])([])",
            );
            assert!(scan(&module, &secret_before_cycle).unwrap());
            // A cycle with a large fan-out holds one frame for each level, so
            // it ends at the depth limit with little memory.
            let code = c"
parent = {'children': []}
parent['children'].extend({'id': index, 'parent': parent} for index in range(2000))
";
            py.run(code, Some(&module.dict()), None).unwrap();
            assert!(is_recursion_error(
                scan(&module, &eval(&module, "parent")).unwrap_err()
            ));

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
