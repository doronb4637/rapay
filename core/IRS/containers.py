"""Containers IRS hands back for array fields.

A parsed array is a real `list` -- editable, `isinstance(x, list)`, and accepted
by `beartype` against a `list[int]` annotation -- because editing a value in
place is the normal thing to do with a parsed message::

    msg.samples[0] = 5

But a `[UInt16, 9]` field is nine items *on the wire*. Growing or shrinking it
is not an edit, it is a layout violation, and today it stays silent until
`ArrayField.to_bytes` refuses to serialize -- long after the line that caused
it. `FixedList` moves that failure to the mutation itself.

Only `length: int` arrays get one. A counted array (`length="Len"`) and a greedy
array (`length=None`) are *meant* to change length, so they stay plain lists.
"""


class FixedList(list):
    """A `list` whose length is frozen. Elements stay mutable.

    >>> a = FixedList([1, 2, 3])
    >>> a[0] = 9                       # fine -- editing a value
    >>> a.append(4)                    # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    TypeError: fixed-length array of 3 items: length cannot change
    """
    __slots__ = ()

    def _locked(self, *args, **kwargs):
        raise TypeError(
            f"fixed-length array of {len(self)} items: length cannot change "
            f"(assign to an index to edit a value)")

    append = extend = insert = pop = remove = clear = _locked
    __delitem__ = __iadd__ = __imul__ = _locked

    def __setitem__(self, index, value) -> None:
        """Index assignment is free; slice assignment must keep the length."""
        if type(index) is slice:
            replaced = len(range(*index.indices(len(self))))
            value = value if type(value) is list else list(value)
            if len(value) != replaced:
                self._locked()
        list.__setitem__(self, index, value)

    # `list` pickles/copies by re-appending into a fresh instance, which our own
    # `append` refuses. Rebuild from a plain list instead.
    def __reduce__(self):
        return (self.__class__, (list(self),))

    def __copy__(self):
        return self.__class__(self)

    def __deepcopy__(self, memo):
        from copy import deepcopy
        return self.__class__(deepcopy(item, memo) for item in self)

    def __repr__(self) -> str:
        return f"FixedList({list.__repr__(self)})"
