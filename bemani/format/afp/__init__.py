import io
from hashlib import md5
import os
import struct
import sys
from PIL import Image  # type: ignore
from typing import Any, Dict, List, Optional, Tuple

from bemani.format.dxt import DXTBuffer
from bemani.protocol.binary import BinaryEncoding
from bemani.protocol.lz77 import Lz77
from bemani.protocol.node import Node

from bemani.format.afp.types import Matrix, Color, Point, Rectangle
from bemani.format.afp.types import AP2Action, AP2Tag, AP2Property


def _hex(data: int) -> str:
    hexval = hex(data)[2:]
    if len(hexval) == 1:
        return "0" + hexval
    return hexval


class PMAN:
    def __init__(
        self,
        entries: List[str] = [],
        ordering: List[int] = [],
        flags1: int = 0,
        flags2: int = 0,
        flags3: int = 0,
    ) -> None:
        self.entries = entries
        self.ordering = ordering
        self.flags1 = flags1
        self.flags2 = flags2
        self.flags3 = flags3

    def as_dict(self) -> Dict[str, Any]:
        return {
            'flags': [self.flags1, self.flags2, self.flags3],
            'entries': self.entries,
            'ordering': self.ordering,
        }


class Texture:
    def __init__(
        self,
        name: str,
        width: int,
        height: int,
        fmt: int,
        header_flags1: int,
        header_flags2: int,
        header_flags3: int,
        fmtflags: int,
        rawdata: bytes,
        compressed: Optional[bytes],
        imgdata: Any,
    ) -> None:
        self.name = name
        self.width = width
        self.height = height
        self.fmt = fmt
        self.header_flags1 = header_flags1
        self.header_flags2 = header_flags2
        self.header_flags3 = header_flags3
        self.fmtflags = fmtflags
        self.raw = rawdata
        self.compressed = compressed
        self.img = imgdata

    def as_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'width': self.width,
            'height': self.height,
            'fmt': self.fmt,
            'header_flags': [self.header_flags1, self.header_flags2, self.header_flags3],
            'fmt_flags': self.fmtflags,
            'raw': "".join(_hex(x) for x in self.raw),
            'compressed': "".join(_hex(x) for x in self.compressed) if self.compressed is not None else None,
        }


class TextureRegion:
    def __init__(self, textureno: int, left: int, top: int, right: int, bottom: int) -> None:
        self.textureno = textureno
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom

    def as_dict(self) -> Dict[str, Any]:
        return {
            'texture': self.textureno,
            'left': self.left,
            'top': self.top,
            'right': self.right,
            'bottom': self.bottom,
        }

    def __repr__(self) -> str:
        return (
            f"texture: {self.textureno}, " +
            f"left: {self.left / 2}, " +
            f"top: {self.top / 2}, " +
            f"right: {self.right / 2}, " +
            f"bottom: {self.bottom / 2}, " +
            f"width: {(self.right - self.left) / 2}, " +
            f"height: {(self.bottom - self.top) / 2}"
        )


class SWF:
    def __init__(
        self,
        name: str,
        data: bytes,
        descramble_info: bytes = b"",
    ) -> None:
        self.name = name
        self.exported_name = ""
        self.data = data
        self.descramble_info = descramble_info

        # Initialize coverage. This is used to help find missed/hidden file
        # sections that we aren't parsing correctly.
        self.coverage: List[bool] = [False] * len(data)

        # Initialize string table. This is used for faster lookup of strings
        # as well as tracking which strings in the table have been parsed correctly.
        self.strings: Dict[int, Tuple[str, bool]] = {}

    def add_coverage(self, offset: int, length: int, unique: bool = True) -> None:
        for i in range(offset, offset + length):
            if self.coverage[i] and unique:
                raise Exception(f"Already covered {hex(offset)}!")
            self.coverage[i] = True

    def print_coverage(self) -> None:
        # First offset that is not coverd in a run.
        start = None

        for offset, covered in enumerate(self.coverage):
            if covered:
                if start is not None:
                    print(f"Uncovered bytes: {hex(start)} - {hex(offset)} ({offset-start} bytes)", file=sys.stderr)
                    start = None
            else:
                if start is None:
                    start = offset
        if start is not None:
            # Print final range
            offset = len(self.coverage)
            print(f"Uncovered bytes: {hex(start)} - {hex(offset)} ({offset-start} bytes)", file=sys.stderr)

        # Now, print uncovered strings
        for offset, (string, covered) in self.strings.items():
            if covered:
                continue

            print(f"Uncovered string: {hex(offset)} - {string}", file=sys.stderr)

    def as_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'data': "".join(_hex(x) for x in self.data),
            'descramble_info': "".join(_hex(x) for x in self.descramble_info),
        }

    def __parse_bytecode(self, datachunk: bytes, string_offsets: List[int] = [], prefix: str = "", verbose: bool = False) -> None:
        # Suppress debug text unless asked
        if verbose:
            def vprint(*args: Any, **kwargs: Any) -> None:  # type: ignore
                print(*args, **kwargs, file=sys.stderr)

            add_coverage = self.add_coverage
        else:
            def vprint(*args: Any, **kwargs: Any) -> None:  # type: ignore
                pass

            def add_coverage(*args: Any, **kwargs: Any) -> None:  # type: ignore
                pass

        # First, we need to check if this is a SWF-style bytecode or an AP2 bytecode.
        ap2_sentinel = struct.unpack("<B", datachunk[0:1])[0]

        if ap2_sentinel != 0xFF:
            raise Exception("Encountered SWF-style bytecode but we don't support this!")

        # Now, we need to grab the flags byte which tells us how to find the actual bytecode.
        flags = struct.unpack("<B", datachunk[1:2])[0]

        if flags & 0x1:
            # There is an offset pointer telling us where the data is as well as string offset tables.
            string_offsets_count = struct.unpack("<H", datachunk[2:4])[0]

            # We don't want to overwrite the global ones with our current ones.
            if not string_offsets:
                string_offsets = list(struct.unpack("<" + ("H" * string_offsets_count), datachunk[4:(4 + (2 * string_offsets_count))]))

            offset_ptr = (string_offsets_count + 2) * 2
        else:
            # The data directly follows, no pointer.
            offset_ptr = 2
        start_offset = offset_ptr

        vprint(f"{prefix}    Flags: {hex(flags)}, Bytecode Actual Offset: {hex(offset_ptr)}")

        # Actually parse out the opcodes:
        while offset_ptr < len(datachunk):
            # We leave it up to the individual opcode handlers to increment the offset pointer. By default, parameterless
            # opcodes increase by one. Everything else increases by its own amount. Opcode parsing here is done in big-endian
            # as the game code seems to always parse big-endian values.
            opcode = struct.unpack(">B", datachunk[offset_ptr:(offset_ptr + 1)])[0]
            action_name = AP2Action.action_to_name(opcode)

            # Because the starting offset is non-zero, we calculate this here as a convenience for displaying. It means
            # that line numbers for opcodes start at 0 but we have to fix up offsets for jumps by the start_offset.
            lineno = offset_ptr - start_offset

            if opcode in AP2Action.actions_without_params():
                vprint(f"{prefix}      {lineno}: {action_name}")
                offset_ptr += 1
            elif opcode == AP2Action.DEFINE_FUNCTION2:
                function_flags, funcname_offset, bytecode_offset, _, bytecode_count = struct.unpack(
                    ">HHHBH",
                    datachunk[(offset_ptr + 1):(offset_ptr + 10)],
                )

                if funcname_offset == 0:
                    funcname = "<anonymous function>"
                else:
                    funcname = self.__get_string(funcname_offset)
                offset_ptr += 10 + (3 * bytecode_offset)

                vprint(f"{prefix}      {lineno}: {action_name} Flags: {hex(function_flags)}, Name: {funcname}, Bytecode Offset: {hex(bytecode_offset)}, Bytecode Length: {hex(bytecode_count)}")
                self.__parse_bytecode(datachunk[offset_ptr:(offset_ptr + bytecode_count)], string_offsets=string_offsets, prefix=prefix + "    ", verbose=verbose)
                vprint(f"{prefix}      END_{action_name}")

                offset_ptr += bytecode_count
            elif opcode == AP2Action.PUSH:
                obj_count = struct.unpack(">B", datachunk[(offset_ptr + 1):(offset_ptr + 2)])[0]
                offset_ptr += 2

                vprint(f"{prefix}      {lineno}: {action_name}")

                while obj_count > 0:
                    obj_to_create = struct.unpack(">B", datachunk[offset_ptr:(offset_ptr + 1)])[0]
                    offset_ptr += 1

                    if obj_to_create == 0x0:
                        # Integer "0" object.
                        vprint(f"{prefix}        INTEGER: 0")
                    elif obj_to_create == 0x1:
                        # Float object, represented internally as a double.
                        fval = struct.unpack(">f", datachunk[offset_ptr:(offset_ptr + 4)])[0]
                        offset_ptr += 4

                        vprint(f"{prefix}        FLOAT: {fval}")
                    elif obj_to_create == 0x2:
                        # Null pointer object.
                        vprint(f"{prefix}        NULL")
                    elif obj_to_create == 0x3:
                        # Undefined constant.
                        vprint(f"{prefix}        UNDEFINED")
                    elif obj_to_create == 0x4:
                        # Register value.
                        regno = struct.unpack(">B", datachunk[offset_ptr:(offset_ptr + 1)])[0]
                        offset_ptr += 1

                        vprint(f"{prefix}        REGISTER NO: {regno}")
                    elif obj_to_create == 0x5:
                        # Boolean "TRUE" object.
                        vprint(f"{prefix}        BOOLEAN: True")
                    elif obj_to_create == 0x6:
                        # Boolean "FALSE" object.
                        vprint(f"{prefix}        BOOLEAN: False")
                    elif obj_to_create == 0x7:
                        # Integer object.
                        ival = struct.unpack(">I", datachunk[offset_ptr:(offset_ptr + 4)])[0]
                        offset_ptr += 4

                        vprint(f"{prefix}        INTEGER: {ival}")
                    elif obj_to_create == 0x8:
                        # String constant object.
                        const_offset = struct.unpack(">B", datachunk[offset_ptr:(offset_ptr + 1)])[0]
                        const = self.__get_string(string_offsets[const_offset])
                        offset_ptr += 1

                        vprint(f"{prefix}        STRING CONST: {const}")
                    elif obj_to_create == 0x9:
                        # String constant, but with 16 bits for the offset. Probably not used except
                        # on the largest files.
                        const_offset = struct.unpack(">H", datachunk[offset_ptr:(offset_ptr + 2)])[0]
                        const = self.__get_string(string_offsets[const_offset])
                        offset_ptr += 2

                        vprint(f"{prefix}        STRING_CONTS: {const}")
                    elif obj_to_create == 0xa:
                        # NaN constant.
                        vprint(f"{prefix}        NAN")
                    elif obj_to_create == 0xb:
                        # Infinity constant.
                        vprint(f"{prefix}        INFINITY")
                    elif obj_to_create == 0xc:
                        # Pointer to "this" object, whatever currently is executing the bytecode.
                        vprint(f"{prefix}        POINTER TO THIS")
                    elif obj_to_create == 0xd:
                        # Pointer to "root" object, which is the movieclip this bytecode exists in.
                        vprint(f"{prefix}        POINTER TO ROOT")
                    elif obj_to_create == 0xe:
                        # Pointer to "parent" object, whatever currently is executing the bytecode.
                        # This seems to be the parent of the movie clip, or the current movieclip
                        # if that isn't set.
                        vprint(f"{prefix}        POINTER TO PARENT")
                    elif obj_to_create == 0xf:
                        # Current movie clip.
                        vprint(f"{prefix}        POINTER TO CURRENT MOVIECLIP")
                    elif obj_to_create == 0x10:
                        # Unknown property name.
                        propertyval = struct.unpack(">B", datachunk[offset_ptr:(offset_ptr + 1)])[0] + 0x100
                        offset_ptr += 1
                        vprint(f"{prefix}        PROPERTY CONST NAME: {AP2Property.property_to_name(propertyval)}")
                    elif obj_to_create == 0x13:
                        # Class property name.
                        propertyval = struct.unpack(">B", datachunk[offset_ptr:(offset_ptr + 1)])[0] + 0x300
                        offset_ptr += 1
                        vprint(f"{prefix}        CLASS CONST NAME: {AP2Property.property_to_name(propertyval)}")
                    elif obj_to_create == 0x16:
                        # Func property name.
                        propertyval = struct.unpack(">B", datachunk[offset_ptr:(offset_ptr + 1)])[0] + 0x400
                        offset_ptr += 1
                        vprint(f"{prefix}        FUNC CONST NAME: {AP2Property.property_to_name(propertyval)}")
                    elif obj_to_create == 0x19:
                        # Other property name.
                        propertyval = struct.unpack(">B", datachunk[offset_ptr:(offset_ptr + 1)])[0] + 0x200
                        offset_ptr += 1
                        vprint(f"{prefix}        OTHER CONST NAME: {AP2Property.property_to_name(propertyval)}")
                    elif obj_to_create == 0x1c:
                        # Event property name.
                        propertyval = struct.unpack(">B", datachunk[offset_ptr:(offset_ptr + 1)])[0] + 0x500
                        offset_ptr += 1
                        vprint(f"{prefix}        EVENT CONST NAME: {AP2Property.property_to_name(propertyval)}")
                    elif obj_to_create == 0x1f:
                        # Key constants.
                        propertyval = struct.unpack(">B", datachunk[offset_ptr:(offset_ptr + 1)])[0] + 0x600
                        offset_ptr += 1
                        vprint(f"{prefix}        KEY CONST NAME: {AP2Property.property_to_name(propertyval)}")
                    elif obj_to_create == 0x22:
                        # Pointer to global object.
                        vprint(f"{prefix}        POINTER TO GLOBAL OBJECT")
                    elif obj_to_create == 0x24:
                        # Some other property name.
                        propertyval = struct.unpack(">B", datachunk[offset_ptr:(offset_ptr + 1)])[0] + 0x700
                        offset_ptr += 1
                        vprint(f"{prefix}        ETC2 CONST NAME: {AP2Property.property_to_name(propertyval)}")
                    elif obj_to_create == 0x27:
                        # Some other property name.
                        propertyval = struct.unpack(">B", datachunk[offset_ptr:(offset_ptr + 1)])[0] + 0x800
                        offset_ptr += 1
                        vprint(f"{prefix}        ORGFUNC2 CONST NAME: {AP2Property.property_to_name(propertyval)}")
                    elif obj_to_create == 0x37:
                        # Integer object but one byte.
                        ival = struct.unpack(">B", datachunk[offset_ptr:(offset_ptr + 1)])[0]
                        offset_ptr += 1

                        vprint(f"{prefix}        INTEGER: {ival}")
                    else:
                        raise Exception(f"Unsupported object {hex(obj_to_create)} to push!")

                    obj_count -= 1

                vprint(f"{prefix}      END_{action_name}")
            elif opcode == AP2Action.STORE_REGISTER:
                obj_count = struct.unpack(">B", datachunk[(offset_ptr + 1):(offset_ptr + 2)])[0]
                offset_ptr += 2

                vprint(f"{prefix}      {lineno}: {action_name}")

                while obj_count > 0:
                    register_no = struct.unpack(">B", datachunk[offset_ptr:(offset_ptr + 1)])[0]
                    offset_ptr += 1
                    obj_count -= 1

                    vprint(f"{prefix}        REGISTER NO: {register_no}")
                vprint(f"{prefix}      END_{action_name}")
            elif opcode == AP2Action.STORE_REGISTER2:
                register_no = struct.unpack(">B", datachunk[(offset_ptr + 1):(offset_ptr + 2)])[0]
                offset_ptr += 2

                vprint(f"{prefix}      {lineno}: {action_name}")
                vprint(f"{prefix}        REGISTER NO: {register_no}")
                vprint(f"{prefix}      END_{action_name}")
            elif opcode == AP2Action.IF:
                jump_if_true_offset = struct.unpack(">H", datachunk[(offset_ptr + 1):(offset_ptr + 3)])[0]
                offset_ptr += 3

                # TODO: This can jump outside of a function definition, most commonly seen when jumping to an
                # "END" pointer at the end of a chunk. We need to handle this. We probably need function lines
                # to be absolute instead of relative.
                jump_if_true_offset += offset_ptr - start_offset

                vprint(f"{prefix}      {lineno}: Offset If True: {jump_if_true_offset}")
            elif opcode == AP2Action.IF2:
                if2_type, jump_if_true_offset = struct.unpack(">BH", datachunk[(offset_ptr + 1):(offset_ptr + 4)])
                offset_ptr += 4

                # TODO: This can jump outside of a function definition, most commonly seen when jumping to an
                # "END" pointer at the end of a chunk. We need to handle this. We probably need function lines
                # to be absolute instead of relative.
                jump_if_true_offset += offset_ptr - start_offset

                if2_typestr = {
                    0: "==",
                    1: "!=",
                    2: "<",
                    3: ">",
                    4: "<=",
                    5: ">=",
                    6: "!",
                    7: "BITAND",
                    8: "BITNOTAND",
                    9: "STRICT ==",
                    10: "STRICT !=",
                    11: "IS UNDEFINED",
                    12: "IS NOT UNDEFINED",
                }[if2_type]

                vprint(f"{prefix}      {lineno}: {action_name} {if2_typestr}, Offset If True: {jump_if_true_offset}")
            elif opcode == AP2Action.JUMP:
                jump_offset = struct.unpack(">H", datachunk[(offset_ptr + 1):(offset_ptr + 3)])[0]
                offset_ptr += 3

                # TODO: This can jump outside of a function definition, most commonly seen when jumping to an
                # "END" pointer at the end of a chunk. We need to handle this. We probably need function lines
                # to be absolute instead of relative.
                jump_offset += offset_ptr - start_offset
                vprint(f"{prefix}      {lineno}: {action_name} Offset: {jump_offset}")
            elif opcode == AP2Action.ADD_NUM_VARIABLE:
                amount_to_add = struct.unpack(">B", datachunk[(offset_ptr + 1):(offset_ptr + 2)])[0]
                offset_ptr += 2

                vprint(f"{prefix}      {lineno}: {action_name} Add Value: {amount_to_add}")
            elif opcode == AP2Action.START_DRAG:
                constraint = struct.unpack(">b", datachunk[(offset_ptr + 1):(offset_ptr + 2)])[0]
                offset_ptr += 2

                vprint(f"{prefix}      {lineno}: {action_name} Constrain Mouse: {'yes' if constraint > 0 else ('no' if constraint == 0 else 'check stack')}")
            elif opcode == AP2Action.ADD_NUM_REGISTER:
                register_no, amount_to_add = struct.unpack(">BB", datachunk[(offset_ptr + 1):(offset_ptr + 3)])
                offset_ptr += 3

                vprint(f"{prefix}      {lineno}: {action_name} Register No: {register_no}, Add Value: {amount_to_add}")
            elif opcode == AP2Action.GOTO_FRAME2:
                flags = struct.unpack(">B", datachunk[(offset_ptr + 1):(offset_ptr + 2)])[0]
                offset_ptr += 2

                if flags & 0x1:
                    post = "STOP"
                else:
                    post = "PLAY"

                if flags & 0x2:
                    # Additional frames to add on top of stack value.
                    additional_frames = struct.unpack(">H", datachunk[offset_ptr:(offset_ptr + 2)])[0]
                    offset_ptr += 2
                else:
                    additional_frames = 0

                vprint(f"{prefix}      {lineno}: {action_name} AND {post} Additional Frames: {additional_frames}")
            else:
                raise Exception(f"Can't advance, no handler for opcode {opcode} ({hex(opcode)})!")

    def __parse_tag(self, ap2_version: int, afp_version: int, ap2data: bytes, tagid: int, size: int, dataoffset: int, prefix: str = "", verbose: bool = False) -> None:
        # Suppress debug text unless asked
        if verbose:
            def vprint(*args: Any, **kwargs: Any) -> None:  # type: ignore
                print(*args, **kwargs, file=sys.stderr)

            add_coverage = self.add_coverage
        else:
            def vprint(*args: Any, **kwargs: Any) -> None:  # type: ignore
                pass

            def add_coverage(*args: Any, **kwargs: Any) -> None:  # type: ignore
                pass

        if tagid == AP2Tag.AP2_SHAPE:
            if size != 4:
                raise Exception(f"Invalid shape size {size}")

            _, shape_id = struct.unpack("<HH", ap2data[dataoffset:(dataoffset + 4)])
            add_coverage(dataoffset, size)

            shape_reference = f"{self.exported_name}_shape{shape_id}"
            vprint(f"{prefix}    Tag ID: {shape_id}, AFP Reference: {shape_reference}, IFS GEO Filename: {md5(shape_reference.encode('utf-8')).hexdigest()}")
        elif tagid == AP2Tag.AP2_DEFINE_SPRITE:
            sprite_flags, sprite_id = struct.unpack("<HH", ap2data[dataoffset:(dataoffset + 4)])
            add_coverage(dataoffset, 4)

            if sprite_flags & 1 == 0:
                # This is an old-style tag, it has data directly following the header.
                subtags_offset = dataoffset + 4
            else:
                # This is a new-style tag, it has a relative data pointer.
                subtags_offset = struct.unpack("<I", ap2data[(dataoffset + 4):(dataoffset + 8)])[0] + dataoffset
                add_coverage(dataoffset + 4, 4)

            vprint(f"{prefix}    Tag ID: {sprite_id}")
            self.__parse_tags(ap2_version, afp_version, ap2data, subtags_offset, prefix="      " + prefix, verbose=verbose)
        elif tagid == AP2Tag.AP2_DEFINE_FONT:
            unk, font_id, fontname_offset, xml_prefix_offset, data_offset, data_count = struct.unpack("<HHHHHH", ap2data[dataoffset:(dataoffset + 12)])
            add_coverage(dataoffset, 12)

            fontname = self.__get_string(fontname_offset)
            xml_prefix = self.__get_string(xml_prefix_offset)

            vprint(f"{prefix}    Tag ID: {font_id}, Font Name: {fontname}, XML Prefix: {xml_prefix}, Entries: {data_count}")

            for i in range(data_count):
                entry_offset = dataoffset + 12 + (data_offset * 2) + (i * 2)
                entry_value = struct.unpack("<H", ap2data[entry_offset:(entry_offset + 2)])[0]
                add_coverage(entry_offset, 2)

                vprint(f"{prefix}      Height: {entry_value}")
        elif tagid == AP2Tag.AP2_DO_ACTION:
            datachunk = ap2data[dataoffset:(dataoffset + size)]
            self.__parse_bytecode(datachunk, prefix=prefix, verbose=verbose)
            add_coverage(dataoffset, size)
        elif tagid == AP2Tag.AP2_PLACE_OBJECT:
            # Allow us to keep track of what we've consumed.
            datachunk = ap2data[dataoffset:(dataoffset + size)]
            flags, depth, object_id = struct.unpack("<IHH", datachunk[0:8])
            add_coverage(dataoffset, 8)

            vprint(f"{prefix}    Flags: {hex(flags)}, Object ID: {object_id}, Depth: {depth}")

            running_pointer = 8
            unhandled_flags = flags

            if flags & 0x2:
                unhandled_flags &= ~0x2
                src_tag_id = struct.unpack("<H", datachunk[running_pointer:(running_pointer + 2)])[0]
                add_coverage(dataoffset + running_pointer, 2)
                running_pointer += 2
                vprint(f"{prefix}    Source Tag ID: {src_tag_id}")

            if flags & 0x10:
                unhandled_flags &= ~0x10
                unk2 = struct.unpack("<H", datachunk[running_pointer:(running_pointer + 2)])[0]
                add_coverage(dataoffset + running_pointer, 2)
                running_pointer += 2
                vprint(f"{prefix}    Unk2: {hex(unk2)}")

            if flags & 0x20:
                unhandled_flags &= ~0x20
                nameoffset = struct.unpack("<H", datachunk[running_pointer:(running_pointer + 2)])[0]
                add_coverage(dataoffset + running_pointer, 2)
                name = self.__get_string(nameoffset)
                running_pointer += 2
                vprint(f"{prefix}    Name: {name}")

            if flags & 0x40:
                unhandled_flags &= ~0x40
                unk3 = struct.unpack("<H", datachunk[running_pointer:(running_pointer + 2)])[0]
                add_coverage(dataoffset + running_pointer, 2)
                running_pointer += 2
                vprint(f"{prefix}    Unk3: {hex(unk3)}")

            if flags & 0x20000:
                unhandled_flags &= ~0x20000
                blend = struct.unpack("<B", datachunk[running_pointer:(running_pointer + 1)])[0]
                add_coverage(dataoffset + running_pointer, 1)
                running_pointer += 1
                vprint(f"{prefix}    Blend: {hex(blend)}")

            # Due to possible misalignment, we need to realign.
            misalignment = running_pointer & 3
            if misalignment > 0:
                catchup = 4 - misalignment
                add_coverage(dataoffset + running_pointer, catchup)
                running_pointer += catchup

            # Handle transformation matrix.
            transform = Matrix.identity()

            if flags & 0x100:
                unhandled_flags &= ~0x100
                a_int, d_int = struct.unpack("<II", datachunk[running_pointer:(running_pointer + 8)])
                add_coverage(dataoffset + running_pointer, 8)
                running_pointer += 8

                transform.a = float(a_int) * 0.0009765625
                transform.d = float(d_int) * 0.0009765625
                vprint(f"{prefix}    Transform Matrix A: {transform.a}, D: {transform.d}")

            if flags & 0x200:
                unhandled_flags &= ~0x200
                b_int, c_int = struct.unpack("<II", datachunk[running_pointer:(running_pointer + 8)])
                add_coverage(dataoffset + running_pointer, 8)
                running_pointer += 8

                transform.b = float(b_int) * 0.0009765625
                transform.c = float(c_int) * 0.0009765625
                vprint(f"{prefix}    Transform Matrix B: {transform.b}, C: {transform.c}")

            if flags & 0x400:
                unhandled_flags &= ~0x400
                tx_int, ty_int = struct.unpack("<II", datachunk[running_pointer:(running_pointer + 8)])
                add_coverage(dataoffset + running_pointer, 8)
                running_pointer += 8

                transform.tx = float(tx_int) / 20.0
                transform.ty = float(tx_int) / 20.0
                vprint(f"{prefix}    Transform Matrix TX: {transform.tx}, TY: {transform.ty}")

            # Handle object colors
            color = Color(1.0, 1.0, 1.0, 1.0)
            acolor = Color(1.0, 1.0, 1.0, 1.0)

            if flags & 0x800:
                unhandled_flags &= ~0x800
                r, g, b, a = struct.unpack("<HHHH", datachunk[running_pointer:(running_pointer + 8)])
                add_coverage(dataoffset + running_pointer, 8)
                running_pointer += 8

                color.r = float(r) * 0.003921569
                color.g = float(g) * 0.003921569
                color.b = float(b) * 0.003921569
                color.a = float(a) * 0.003921569
                vprint(f"{prefix}    Color: {color}")

            if flags & 0x1000:
                unhandled_flags &= ~0x1000
                r, g, b, a = struct.unpack("<HHHH", datachunk[running_pointer:(running_pointer + 8)])
                add_coverage(dataoffset + running_pointer, 8)
                running_pointer += 8

                acolor.r = float(r) * 0.003921569
                acolor.g = float(g) * 0.003921569
                acolor.b = float(b) * 0.003921569
                acolor.a = float(a) * 0.003921569
                vprint(f"{prefix}    AColor: {color}")

            if flags & 0x2000:
                unhandled_flags &= ~0x2000
                rgba = struct.unpack("<I", datachunk[running_pointer:(running_pointer + 4)])[0]
                add_coverage(dataoffset + running_pointer, 4)
                running_pointer += 4

                color.r = float((rgba >> 24) & 0xFF) * 0.003921569
                color.g = float((rgba >> 16) & 0xFF) * 0.003921569
                color.b = float((rgba >> 8) & 0xFF) * 0.003921569
                color.a = float(rgba & 0xFF) * 0.003921569
                vprint(f"{prefix}    Color: {color}")

            if flags & 0x4000:
                unhandled_flags &= ~0x4000
                rgba = struct.unpack("<I", datachunk[running_pointer:(running_pointer + 4)])[0]
                add_coverage(dataoffset + running_pointer, 4)
                running_pointer += 4

                acolor.r = float((rgba >> 24) & 0xFF) * 0.003921569
                acolor.g = float((rgba >> 16) & 0xFF) * 0.003921569
                acolor.b = float((rgba >> 8) & 0xFF) * 0.003921569
                acolor.a = float(rgba & 0xFF) * 0.003921569
                vprint(f"{prefix}    AColor: {color}")

            if flags & 0x80:
                # Object event triggers.
                unhandled_flags &= ~0x80
                event_flags, event_size = struct.unpack("<II", datachunk[running_pointer:(running_pointer + 8)])
                add_coverage(dataoffset + running_pointer, 8)

                if event_flags != 0:
                    _, count = struct.unpack("<HH", datachunk[(running_pointer + 8):(running_pointer + 12)])
                    add_coverage(dataoffset + running_pointer + 8, 4)

                    # The game does not seem to care about length here, but we do, so let's calculate
                    # offsets and use that for lengths.
                    bytecode_offsets: List[int] = []
                    for evt in range(count):
                        evt_offset = running_pointer + 12 + (evt * 8)
                        bytecode_offset = struct.unpack("<H", datachunk[(evt_offset + 6):(evt_offset + 8)])[0] + evt_offset
                        bytecode_offsets.append(bytecode_offset)
                    bytecode_offsets.append(event_size + running_pointer)

                    beginning_to_end: Dict[int, int] = {}
                    for i, bytecode_offset in enumerate(bytecode_offsets[:-1]):
                        beginning_to_end[bytecode_offset] = bytecode_offsets[i + 1]

                    vprint(f"{prefix}    Event Triggers, Count: {count}")
                    for evt in range(count):
                        evt_offset = running_pointer + 12 + (evt * 8)
                        evt_flags, _, keycode, bytecode_offset = struct.unpack("<IBBH", datachunk[evt_offset:(evt_offset + 8)])
                        add_coverage(dataoffset + evt_offset, 8)

                        events: List[str] = []
                        if evt_flags & 0x1:
                            events.append("ON_LOAD")
                        if evt_flags & 0x2:
                            events.append("ON_ENTER_FRAME")
                        if evt_flags & 0x4:
                            events.append("ON_UNLOAD")
                        if evt_flags & 0x8:
                            events.append("ON_MOUSE_MOVE")
                        if evt_flags & 0x10:
                            events.append("ON_MOUSE_DOWN")
                        if evt_flags & 0x20:
                            events.append("ON_MOUSE_UP")
                        if evt_flags & 0x40:
                            events.append("ON_KEY_DOWN")
                        if evt_flags & 0x80:
                            events.append("ON_KEY_UP")
                        if evt_flags & 0x100:
                            events.append("ON_DATA")
                        if evt_flags & 0x400:
                            events.append("ON_PRESS")
                        if evt_flags & 0x800:
                            events.append("ON_RELEASE")
                        if evt_flags & 0x1000:
                            events.append("ON_RELEASE_OUTSIDE")
                        if evt_flags & 0x2000:
                            events.append("ON_ROLL_OVER")
                        if evt_flags & 0x4000:
                            events.append("ON_ROLL_OUT")

                        bytecode_offset += evt_offset
                        bytecode_length = beginning_to_end[bytecode_offset] - bytecode_offset

                        vprint(f"{prefix}      Flags: {hex(evt_flags)} ({', '.join(events)}), KeyCode: {hex(keycode)}, Bytecode Offset: {hex(dataoffset + bytecode_offset)}, Length: {bytecode_length}")
                        self.__parse_bytecode(datachunk[bytecode_offset:(bytecode_offset + bytecode_length)], prefix=prefix + "    ", verbose=verbose)
                        add_coverage(dataoffset + bytecode_offset, bytecode_length)

                running_pointer += event_size

            if flags & 0x10000:
                # Some sort of filter data? Not sure what this is either. Needs more investigation
                # if I encounter files with it.
                unhandled_flags &= ~0x10000
                count, filter_size = struct.unpack("<HH", datachunk[running_pointer:(running_pointer + 4)])
                add_coverage(dataoffset + running_pointer, 4)
                running_pointer += filter_size

                # TODO: This is not understood at all. I need to find data that uses it to continue.
                # running_pointer + 4 starts a series of shorts (exactly count of them) which are
                # all in the range of 0-7, corresponding to some sort of filter. They get sizes
                # looked up and I presume there's data following this corresponding to those sizes.
                # I don't know however as I've not encountered data with this bit.
                vprint(f"{prefix}    Unknown Filter data Count: {count}, Size: {filter_size}")

            if flags & 0x1000000:
                # Some sort of point, perhaps an x, y offset for the object?
                unhandled_flags &= ~0x1000000
                x, y = struct.unpack("<ff", datachunk[running_pointer:(running_pointer + 8)])
                add_coverage(dataoffset + running_pointer, 8)
                running_pointer += 8

                # TODO: This doesn't seem right when run past Pop'n Music data.
                point = Point(x / 20.0, y / 20.0)
                vprint(f"{prefix}    Point: {point}")

            if flags & 0x2000000:
                # Same as above, but initializing to 0, 0 instead of from data.
                unhandled_flags &= ~0x2000000
                point = Point(0.0, 0.0)
                vprint(f"{prefix}    Point: {point}")

            if flags & 0x40000:
                # Some pair of shorts, not sure, its in DDR PS3 data.
                unhandled_flags &= ~0x40000
                x, y = struct.unpack("<HH", datachunk[running_pointer:(running_pointer + 4)])
                add_coverage(dataoffset + running_pointer, 4)
                running_pointer += 4

                # TODO: I have no idea what these are.
                point = Point(x * 3.051758e-05, y * 3.051758e-05)
                vprint(f"{prefix}    Point: {point}")

            if flags & 0x80000:
                # Some pair of shorts, not sure, its in DDR PS3 data.
                unhandled_flags &= ~0x80000
                x, y = struct.unpack("<HH", datachunk[running_pointer:(running_pointer + 4)])
                add_coverage(dataoffset + running_pointer, 4)
                running_pointer += 4

                # TODO: I have no idea what these are.
                point = Point(x * 3.051758e-05, y * 3.051758e-05)
                vprint(f"{prefix}    Point: {point}")

            # This flag states whether we are creating a new object on this depth, or updating one.
            unhandled_flags &= ~0xD
            if flags & 0x1:
                vprint(f"{prefix}    Update object request")
            else:
                vprint(f"{prefix}    Create object request")
            if flags & 0x4:
                vprint(f"{prefix}    Use transform matrix")
            else:
                vprint(f"{prefix}    Ignore transform matrix")
            if flags & 0x4:
                vprint(f"{prefix}    Use color information")
            else:
                vprint(f"{prefix}    Ignore color information")

            if unhandled_flags != 0:
                raise Exception(f"Did not handle {hex(unhandled_flags)} flag bits!")
            if running_pointer < size:
                raise Exception(f"Did not consume {size - running_pointer} bytes ({[hex(x) for x in datachunk[running_pointer:]]}) in object instantiation!")
            if running_pointer != size:
                raise Exception("Logic error!")

        elif tagid == AP2Tag.AP2_REMOVE_OBJECT:
            if size != 4:
                raise Exception(f"Invalid shape size {size}")

            object_id, depth = struct.unpack("<HH", ap2data[dataoffset:(dataoffset + 4)])
            vprint(f"{prefix}    Object ID: {object_id}, Depth: {depth}")
            add_coverage(dataoffset, 4)
        elif tagid == AP2Tag.AP2_DEFINE_EDIT_TEXT:
            if size != 44:
                raise Exception("Invalid size {size} to get data from AP2_DEFINE_EDIT_TEXT!")

            flags, edit_text_id, defined_font_tag_id, font_height, unk_str2_offset = struct.unpack("<IHHHH", ap2data[dataoffset:(dataoffset + 12)])
            add_coverage(dataoffset, 12)

            unk1, unk2, unk3, unk4 = struct.unpack("<HHHH", ap2data[(dataoffset + 12):(dataoffset + 20)])
            add_coverage(dataoffset + 12, 8)

            rgba, f1, f2, f3, f4, variable_name_offset, default_text_offset = struct.unpack("<IiiiiHH", ap2data[(dataoffset + 20):(dataoffset + 44)])
            add_coverage(dataoffset + 20, 24)

            vprint(f"{prefix}    Tag ID: {edit_text_id}, Font Tag: {defined_font_tag_id}, Height Selection: {font_height}, Flags: {hex(flags)}")

            unk_string2 = self.__get_string(unk_str2_offset) or None
            vprint(f"{prefix}      Unk String: {unk_string2}")

            rect = Rectangle(f1 / 20.0, f2 / 20.0, f3 / 20.0, f4 / 20.0)
            vprint(f"{prefix}      Rectangle: {rect}")

            variable_name = self.__get_string(variable_name_offset) or None
            vprint(f"{prefix}      Variable Name: {variable_name}")

            color = Color(
                r=(rgba & 0xFF) / 255.0,
                g=((rgba >> 8) & 0xFF) / 255.0,
                b=((rgba >> 16) & 0xFF) / 255.0,
                a=((rgba >> 24) & 0xFF) / 255.0,
            )
            vprint(f"{prefix}      Text Color: {color}")

            vprint(f"{prefix}      Unk1: {unk1}, Unk2: {unk2}, Unk3: {unk3}, Unk4: {unk4}")

            # flags & 0x20 means something with offset 16-18.
            # flags & 0x200 is unk str below is a HTML tag.

            if flags & 0x80:
                # Has some sort of string pointer.
                default_text = self.__get_string(default_text_offset) or None
                vprint(f"{prefix}      Default Text: {default_text}")
        else:
            raise Exception(f"Unimplemented tag {hex(tagid)}!")

    def __parse_tags(self, ap2_version: int, afp_version: int, ap2data: bytes, tags_base_offset: int, prefix: str = "", verbose: bool = False) -> None:
        # Suppress debug text unless asked
        if verbose:
            def vprint(*args: Any, **kwargs: Any) -> None:  # type: ignore
                print(*args, **kwargs, file=sys.stderr)

            add_coverage = self.add_coverage
        else:
            def vprint(*args: Any, **kwargs: Any) -> None:  # type: ignore
                pass

            def add_coverage(*args: Any, **kwargs: Any) -> None:  # type: ignore
                pass

        unknown_tags_flags, unknown_tags_count, frame_count, tags_count, unknown_tags_offset, frame_offset, tags_offset = struct.unpack(
            "<HHIIIII",
            ap2data[tags_base_offset:(tags_base_offset + 24)]
        )
        add_coverage(tags_base_offset, 24)

        # Fix up pointers.
        tags_offset += tags_base_offset
        unknown_tags_offset += tags_base_offset
        frame_offset += tags_base_offset

        # First, parse regular tags.
        vprint(f"{prefix}Number of Tags: {tags_count}")
        for i in range(tags_count):
            tag = struct.unpack("<I", ap2data[tags_offset:(tags_offset + 4)])[0]
            add_coverage(tags_offset, 4)

            tagid = (tag >> 22) & 0x3FF
            size = tag & 0x3FFFFF

            if size > 0x200000:
                raise Exception(f"Invalid tag size {size} ({hex(size)})")

            vprint(f"{prefix}  Tag: {hex(tagid)} ({AP2Tag.tag_to_name(tagid)}), Size: {hex(size)}, Offset: {hex(tags_offset + 4)}")
            self.__parse_tag(ap2_version, afp_version, ap2data, tagid, size, tags_offset + 4, prefix=prefix, verbose=verbose)
            tags_offset += ((size + 3) & 0xFFFFFFFC) + 4  # Skip past tag header and data, rounding to the nearest 4 bytes.

        # Now, parse frames.
        vprint(f"{prefix}Number of Frames: {frame_count}")
        for i in range(frame_count):
            frame_info = struct.unpack("<I", ap2data[frame_offset:(frame_offset + 4)])[0]
            add_coverage(frame_offset, 4)

            start_tag_id = frame_info & 0xFFFFF
            num_tags_to_play = (frame_info >> 20) & 0xFFF

            vprint(f"{prefix}  Frame Start Tag: {hex(start_tag_id)}, Count: {num_tags_to_play}")
            frame_offset += 4

        # Now, parse unknown tags? I have no idea what these are, but they're referencing strings that
        # are otherwise unused.
        vprint(f"{prefix}Number of Unknown Tags: {unknown_tags_count}, Flags: {hex(unknown_tags_flags)}")
        for i in range(unknown_tags_count):
            unk1, stringoffset = struct.unpack("<HH", ap2data[unknown_tags_offset:(unknown_tags_offset + 4)])
            strval = self.__get_string(stringoffset)
            add_coverage(unknown_tags_offset, 4)

            vprint(f"{prefix}  Unknown Tag: {hex(unk1)} Name: {strval}")
            unknown_tags_offset += 4

    def __descramble(self, scrambled_data: bytes, descramble_info: bytes) -> bytes:
        swap_len = {
            1: 2,
            2: 4,
            3: 8,
        }

        data = bytearray(scrambled_data)
        data_offset = 0
        for i in range(0, len(descramble_info), 2):
            swapword = struct.unpack("<H", descramble_info[i:(i + 2)])[0]
            if swapword == 0:
                break

            offset = (swapword & 0x7F) * 2
            swap_type = (swapword >> 13) & 0x7
            loops = ((swapword >> 7) & 0x3F)
            data_offset += offset

            if swap_type == 0:
                # Just jump forward based on loops
                data_offset += 256 * loops
                continue

            if swap_type not in swap_len:
                raise Exception(f"Unknown swap type {swap_type}!")

            # Reverse the bytes
            for _ in range(loops + 1):
                data[data_offset:(data_offset + swap_len[swap_type])] = data[data_offset:(data_offset + swap_len[swap_type])][::-1]
                data_offset += swap_len[swap_type]

        return bytes(data)

    def __descramble_stringtable(self, scrambled_data: bytes, stringtable_offset: int, stringtable_size: int) -> bytes:
        data = bytearray(scrambled_data)
        curstring: List[int] = []
        curloc = stringtable_offset

        addition = 128
        for i in range(stringtable_size):
            byte = (data[stringtable_offset + i] - addition) & 0xFF
            data[stringtable_offset + i] = byte
            addition += 1

            if byte == 0:
                if curstring:
                    # We found a string!
                    self.strings[curloc - stringtable_offset] = (bytes(curstring).decode('utf8'), False)
                    curloc = stringtable_offset + i + 1
                    curstring = []
                curloc = stringtable_offset + i + 1
            else:
                curstring.append(byte)

        if curstring:
            raise Exception("Logic error!")

        if 0 in self.strings:
            raise Exception("Should not include null string!")

        return bytes(data)

    def __get_string(self, offset: int) -> str:
        if offset == 0:
            return ""

        self.strings[offset] = (self.strings[offset][0], True)
        return self.strings[offset][0]

    def parse(self, verbose: bool = False) -> None:
        # Suppress debug text unless asked
        if verbose:
            def vprint(*args: Any, **kwargs: Any) -> None:  # type: ignore
                print(*args, **kwargs, file=sys.stderr)

            add_coverage = self.add_coverage

            # Reinitialize coverage.
            self.coverage = [False] * len(self.data)
            self.strings = {}
        else:
            def vprint(*args: Any, **kwargs: Any) -> None:  # type: ignore
                pass

            def add_coverage(*args: Any, **kwargs: Any) -> None:  # type: ignore
                pass

        # First, use the byteswap header to descramble the data.
        data = self.__descramble(self.data, self.descramble_info)

        # Start with the basic file header.
        magic, length, version, nameoffset, flags, left, right, top, bottom = struct.unpack("<4sIHHIHHHH", data[0:24])
        width = right - left
        height = bottom - top
        add_coverage(0, 24)

        ap2_data_version = magic[0] & 0xFF
        magic = bytes([magic[3] & 0x7F, magic[2] & 0x7F, magic[1] & 0x7F, 0x0])
        if magic != b'AP2\x00':
            raise Exception(f"Unrecognzied magic {magic}!")
        if length != len(data):
            raise Exception(f"Unexpected length in AFP header, {length} != {len(data)}!")
        if ap2_data_version not in [8, 9, 10]:
            raise Exception(f"Unsupported AP2 container version {ap2_data_version}!")
        if version != 0x200:
            raise Exception(f"Unsupported AP2 version {version}!")

        if flags & 0x1:
            # This appears to be the animation background color.
            rgba = struct.unpack("<I", data[28:32])[0]
            swf_color = Color(
                r=(rgba & 0xFF) / 255.0,
                g=((rgba >> 8) & 0xFF) / 255.0,
                b=((rgba >> 16) & 0xFF) / 255.0,
                a=((rgba >> 24) & 0xFF) / 255.0,
            )
        else:
            swf_color = None
        add_coverage(28, 4)

        if flags & 0x2:
            # FPS can be either an integer or a float.
            fps = struct.unpack("<i", data[24:28])[0] * 0.0009765625
        else:
            fps = struct.unpack("<f", data[24:28])[0]
        add_coverage(24, 4)

        if flags & 0x4:
            # This seems related to imported tags.
            imported_tag_initializers_offset = struct.unpack("<I", data[56:60])[0]
            add_coverage(56, 4)
        else:
            # Unknown offset is not present.
            imported_tag_initializers_offset = None

        # String table
        stringtable_offset, stringtable_size = struct.unpack("<II", data[48:56])
        add_coverage(48, 8)

        # Descramble string table.
        data = self.__descramble_stringtable(data, stringtable_offset, stringtable_size)
        add_coverage(stringtable_offset, stringtable_size)

        # Get exported SWF name.
        self.exported_name = self.__get_string(nameoffset)
        add_coverage(nameoffset + stringtable_offset, len(self.exported_name) + 1, unique=False)
        vprint(f"{os.linesep}AFP name: {self.name}")
        vprint(f"Container Version: {hex(ap2_data_version)}")
        vprint(f"Version: {hex(version)}")
        vprint(f"Exported Name: {self.exported_name}")
        vprint(f"SWF Flags: {hex(flags)}")
        if flags & 0x1:
            vprint(f"  0x1: Movie background color: {swf_color}")
        else:
            vprint("  0x2: No movie background color")
        if flags & 0x2:
            vprint("  0x2: FPS is an integer")
        else:
            vprint("  0x2: FPS is a float")
        if flags & 0x4:
            vprint("  0x4: Imported tag initializer section present")
        else:
            vprint("  0x4: Imported tag initializer section not present")
        vprint(f"Dimensions: {width}x{height}")
        vprint(f"Requested FPS: {fps}")

        # Exported assets
        num_exported_assets = struct.unpack("<H", data[32:34])[0]
        asset_offset = struct.unpack("<I", data[40:44])[0]
        add_coverage(32, 2)
        add_coverage(40, 4)

        # Parse exported asset tag names and their tag IDs.
        vprint(f"Number of Exported Tags: {num_exported_assets}")
        for assetno in range(num_exported_assets):
            asset_data_offset, asset_string_offset = struct.unpack("<HH", data[asset_offset:(asset_offset + 4)])
            add_coverage(asset_offset, 4)
            asset_offset += 4

            asset_name = self.__get_string(asset_string_offset)
            add_coverage(asset_string_offset + stringtable_offset, len(asset_name) + 1, unique=False)
            vprint(f"  {assetno}: Tag Name: {asset_name} Tag ID: {asset_data_offset}")

        # Tag sections
        tags_offset = struct.unpack("<I", data[36:40])[0]
        add_coverage(36, 4)
        self.__parse_tags(ap2_data_version, version, data, tags_offset, verbose=verbose)

        # Imported tags sections
        imported_tags_count = struct.unpack("<h", data[34:36])[0]
        imported_tags_offset = struct.unpack("<I", data[44:48])[0]
        imported_tags_data_offset = imported_tags_offset + 4 * imported_tags_count
        add_coverage(34, 2)
        add_coverage(44, 4)

        vprint(f"Number of Imported Tags: {imported_tags_count}")
        for i in range(imported_tags_count):
            # First grab the SWF this is importing from, and the number of assets being imported.
            swf_name_offset, count = struct.unpack("<HH", data[imported_tags_offset:(imported_tags_offset + 4)])
            add_coverage(imported_tags_offset, 4)

            swf_name = self.__get_string(swf_name_offset)
            add_coverage(swf_name_offset + stringtable_offset, len(swf_name) + 1, unique=False)
            vprint(f"  Source SWF: {swf_name}")

            # Now, grab the actual asset names being imported.
            for j in range(count):
                asset_id_no, asset_name_offset = struct.unpack("<HH", data[imported_tags_data_offset:(imported_tags_data_offset + 4)])
                add_coverage(imported_tags_data_offset, 4)

                asset_name = self.__get_string(asset_name_offset)
                add_coverage(asset_name_offset + stringtable_offset, len(asset_name) + 1, unique=False)
                vprint(f"    Tag ID: {asset_id_no}, Requested Asset: {asset_name}")

                imported_tags_data_offset += 4

            imported_tags_offset += 4

        # This appears to be bytecode to execute on a per-frame basis. We execute this every frame and
        # only execute up to the point where we equal the current frame.
        if imported_tag_initializers_offset is not None:

            unk1, length = struct.unpack("<HH", data[imported_tag_initializers_offset:(imported_tag_initializers_offset + 4)])
            add_coverage(imported_tag_initializers_offset, 4)

            vprint(f"Imported Tag Initializer Offset: {hex(imported_tag_initializers_offset)}, Length: {length}")

            for i in range(length):
                item_offset = imported_tag_initializers_offset + 4 + (i * 12)
                tag_id, frame, action_bytecode_offset, action_bytecode_length = struct.unpack("<HHII", data[item_offset:(item_offset + 12)])
                add_coverage(item_offset, 12)

                if action_bytecode_length != 0:
                    vprint(f"  Tag ID: {tag_id}, Frame: {frame}, Bytecode Offset: {hex(action_bytecode_offset + imported_tag_initializers_offset)}")
                    bytecode_data = data[(action_bytecode_offset + imported_tag_initializers_offset):(action_bytecode_offset + imported_tag_initializers_offset + action_bytecode_length)]
                    self.__parse_bytecode(bytecode_data, verbose=verbose)
                else:
                    vprint(f"  Tag ID: {tag_id}, Frame: {frame}, No Bytecode Present")

        if verbose:
            self.print_coverage()


class DrawParams:
    def __init__(
        self,
        flags: int,
        region: Optional[str] = None,
        vertexes: List[int] = [],
        blend: Optional[Color] = None,
    ) -> None:
        self.flags = flags
        self.region = region
        self.vertexes = vertexes
        self.blend = blend

    def as_dict(self) -> Dict[str, Any]:
        return {
            'flags': self.flags,
            'region': self.region,
            'vertexes': self.vertexes,
            'blend': self.blend.as_dict() if self.blend else None,
        }

    def __repr__(self) -> str:
        flagbits: List[str] = []
        if self.flags & 0x1:
            flagbits.append("(Instantiable)")
        if self.flags & 0x2:
            flagbits.append("(Includes Texture)")
        if self.flags & 0x4:
            flagbits.append("(Includes Texture Color)")
        if self.flags & 0x8:
            flagbits.append("(Includes Blend Color)")
        if self.flags & 0x40:
            flagbits.append("(Needs Tex Point Normalization)")

        flagspart = f"flags: {hex(self.flags)} {' '.join(flagbits)}"
        if self.flags & 0x2:
            texpart = f", region: {self.region}, vertexes: {', '.join(str(x) for x in self.vertexes)}"
        else:
            texpart = ""

        if self.flags & 0x8:
            blendpart = f", blend: {self.blend}"
        else:
            blendpart = ""

        return f"{flagspart}{texpart}{blendpart}"


class Shape:
    def __init__(
        self,
        name: str,
        data: bytes,
    ) -> None:
        self.name = name
        self.data = data

        # Vertex points outlining this shape.
        self.vertex_points: List[Point] = []

        # Texture points, as used alongside vertex chunks when the shape contains a texture.
        self.tex_points: List[Point] = []

        # Colors for texture points, if they exist in the file.
        self.tex_colors: List[Color] = []

        # Actual shape drawing parameters.
        self.draw_params: List[DrawParams] = []

    def as_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'vertex_points': [p.as_dict() for p in self.vertex_points],
            'tex_points': [p.as_dict() for p in self.tex_points],
            'tex_colors': [c.as_dict() for c in self.tex_colors],
            'draw_params': [d.as_dict() for d in self.draw_params],
        }

    def __repr__(self) -> str:
        return os.linesep.join([
            *[f"vertex point: {vertex}" for vertex in self.vertex_points],
            *[f"tex point: {tex}" for tex in self.tex_points],
            *[f"tex color: {color}" for color in self.tex_colors],
            *[f"draw params: {params}" for params in self.draw_params],
        ])

    def get_until_null(self, offset: int) -> bytes:
        out = b""
        while self.data[offset] != 0:
            out += self.data[offset:(offset + 1)]
            offset += 1
        return out

    def parse(self, text_obfuscated: bool = True) -> None:
        # First, grab the header bytes.
        magic = self.data[0:4]

        if magic == b"D2EG":
            endian = "<"
        elif magic == b"GE2D":
            endian = ">"
        else:
            raise Exception("Invalid magic value in GE2D structure!")

        # There are two integers at 0x4 and 0x8 which are basically file versions.

        filesize = struct.unpack(f"{endian}I", self.data[12:16])[0]
        if filesize != len(self.data):
            raise Exception("Unexpected file size for GE2D structure!")

        # There is an integer at 0x16 which always appears to be zero. It should be
        # file flags, but I don't know what it does since no code I've found cares.
        if self.data[16:20] != b"\0\0\0\0":
            raise Exception("Unhandled flag data bytes in GE2D structure!")

        vertex_count, tex_count, color_count, label_count, render_params_count, _ = struct.unpack(
            f"{endian}HHHHHH",
            self.data[20:32],
        )

        vertex_offset, tex_offset, color_offset, label_offset, render_params_offset = struct.unpack(
            f"{endian}IIIII",
            self.data[32:52],
        )

        vertex_points: List[Point] = []
        if vertex_offset != 0:
            for vertexno in range(vertex_count):
                vertexno_offset = vertex_offset + (8 * vertexno)
                x, y = struct.unpack(f"{endian}ff", self.data[vertexno_offset:vertexno_offset + 8])
                vertex_points.append(Point(x, y))
        self.vertex_points = vertex_points

        tex_points: List[Point] = []
        if tex_offset != 0:
            for texno in range(tex_count):
                texno_offset = tex_offset + (8 * texno)
                x, y = struct.unpack(f"{endian}ff", self.data[texno_offset:texno_offset + 8])
                tex_points.append(Point(x, y))
        self.tex_points = tex_points

        colors: List[Color] = []
        if color_offset != 0:
            for colorno in range(color_count):
                colorno_offset = color_offset + (4 * colorno)
                rgba = struct.unpack(f"{endian}I", self.data[colorno_offset:colorno_offset + 4])[0]
                color = Color(
                    a=(rgba & 0xFF) / 255.0,
                    b=((rgba >> 8) & 0xFF) / 255.0,
                    g=((rgba >> 16) & 0xFF) / 255.0,
                    r=((rgba >> 24) & 0xFF) / 255.0,
                )
                colors.append(color)
        self.tex_colors = colors

        labels: List[str] = []
        if label_offset != 0:
            for labelno in range(label_count):
                labelno_offset = label_offset + (4 * labelno)
                labelptr = struct.unpack(f"{endian}I", self.data[labelno_offset:labelno_offset + 4])[0]

                bytedata = self.get_until_null(labelptr)
                labels.append(AFPFile.descramble_text(bytedata, text_obfuscated))

        draw_params: List[DrawParams] = []
        if render_params_offset != 0:
            # The actual render parameters for the shape. This dictates how the texture values
            # are used when drawing shapes, whether to use a blend value or draw a primitive, etc.
            for render_paramsno in range(render_params_count):
                render_paramsno_offset = render_params_offset + (16 * render_paramsno)
                mode, flags, tex1, tex2, trianglecount, _, rgba, triangleoffset = struct.unpack(
                    f"{endian}BBBBHHII",
                    self.data[(render_paramsno_offset):(render_paramsno_offset + 16)]
                )

                if mode != 4:
                    raise Exception("Unexpected mode in GE2D structure!")
                if (flags & 0x2) and len(labels) == 0:
                    raise Exception("GE2D structure has a texture, but no region labels present!")
                if (flags & 0x2) and (tex1 == 0xFF):
                    raise Exception("GE2D structure requests a texture, but no texture pointer present!")
                if tex2 != 0xFF:
                    raise Exception("GE2D structure requests a second texture, but we don't support this!")

                color = Color(
                    r=(rgba & 0xFF) / 255.0,
                    g=((rgba >> 8) & 0xFF) / 255.0,
                    b=((rgba >> 16) & 0xFF) / 255.0,
                    a=((rgba >> 24) & 0xFF) / 255.0,
                )

                verticies: List[int] = []
                for render_paramstriangleno in range(trianglecount):
                    render_paramstriangleno_offset = triangleoffset + (2 * render_paramstriangleno)
                    tex_offset = struct.unpack(f"{endian}H", self.data[render_paramstriangleno_offset:(render_paramstriangleno_offset + 2)])[0]
                    verticies.append(tex_offset)

                # Seen bits are 0x1, 0x2, 0x4, 0x8 so far.
                # 0x1 Is a "this shape is instantiable/drawable" bit.
                # 0x2 Is the shape having a texture.
                # 0x4 Is the shape having a texture color per texture point.
                # 0x8 Is "draw background color/blend" flag.
                # 0x40 Is a "normalize texture coordinates" flag. It performs the below algorithm.

                if (flags & (0x2 | 0x40)) == (0x2 | 0x40):
                    # The tex offsets point at the tex vals parsed above, and are used in conjunction with
                    # texture/region metrics to calcuate some offsets. First, the region left/right/top/bottom
                    # is divided by 2 (looks like a scaling of 2 for regions to textures is hardcoded) and then
                    # divided by the texture width/height (as relevant). The returned metrics are in texture space
                    # where 0.0 is the origin and 1.0 is the furthest right/down. The metrics are then multiplied
                    # by the texture point pairs that appear above, meaning they should be treated as percentages.
                    pass

                draw_params.append(
                    DrawParams(
                        flags=flags,
                        region=labels[tex1] if (flags & 0x2) else None,
                        vertexes=verticies if (flags & 0x6) else [],
                        blend=color if (flags & 0x8) else None,
                    )
                )
        self.draw_params = draw_params


class Unknown1:
    def __init__(
        self,
        name: str,
        data: bytes,
    ) -> None:
        self.name = name
        self.data = data
        if len(data) != 12:
            raise Exception("Unexpected length for Unknown1 structure!")

    def as_dict(self) -> Dict[str, Any]:
        return {
            'name': self.name,
            'data': "".join(_hex(x) for x in self.data),
        }


class Unknown2:
    def __init__(
        self,
        data: bytes,
    ) -> None:
        self.data = data
        if len(data) != 4:
            raise Exception("Unexpected length for Unknown2 structure!")

    def as_dict(self) -> Dict[str, Any]:
        return {
            'data': "".join(_hex(x) for x in self.data),
        }


class AFPFile:
    def __init__(self, contents: bytes, verbose: bool = False) -> None:
        # Initialize coverage. This is used to help find missed/hidden file
        # sections that we aren't parsing correctly.
        self.coverage: List[bool] = [False] * len(contents)

        # Original file data that we parse into structures.
        self.data = contents

        # Font data encoding handler. We keep this around as it manages
        # remembering the actual BinXML encoding.
        self.benc = BinaryEncoding()

        # All of the crap!
        self.endian: str = "<"
        self.features: int = 0
        self.file_flags: bytes = b""
        self.text_obfuscated: bool = False
        self.legacy_lz: bool = False
        self.modern_lz: bool = False

        # If we encounter parts of the file that we don't know how to read
        # or save, we drop into read-only mode and throw if somebody tries
        # to update the file.
        self.read_only: bool = False

        # List of all textures in this file. This is unordered, textures should
        # be looked up by name.
        self.textures: List[Texture] = []

        # Texture mapping, which allows other structures to refer to texture
        # by number instead of name.
        self.texturemap: PMAN = PMAN()

        # List of all regions found inside textures, mapped to their textures
        # using texturenos that can be looked up using the texturemap above.
        # This structure is ordered, and the regionno from the regionmap
        # below can be used to look into this structure.
        self.texture_to_region: List[TextureRegion] = []

        # Region mapping, which allows other structures to refer to regions
        # by number instead of name.
        self.regionmap: PMAN = PMAN()

        # Level data (swf-derivative) and their names found in this file. This is
        # unordered, swfdata should be looked up by name.
        self.swfdata: List[SWF] = []

        # Level data (swf-derivative) mapping, which allows other structures to
        # refer to swfdata by number instead of name.
        self.swfmap: PMAN = PMAN()

        # Font information (mapping for various coepoints to their region in
        # a particular font texture.
        self.fontdata: Optional[Node] = None

        # Shapes(?) with their raw data.
        self.shapes: List[Shape] = []

        # Shape(?) mapping, not understood or used.
        self.shapemap: PMAN = PMAN()

        # Unknown data structures that we have to roundtrip. They correlate to
        # the PMAN structures below.
        self.unknown1: List[Unknown1] = []
        self.unknown2: List[Unknown2] = []

        # Unknown PMAN structures that we have to roundtrip. They correlate to
        # the unknown data structures above.
        self.unk_pman1: PMAN = PMAN()
        self.unk_pman2: PMAN = PMAN()

        # Parse out the file structure.
        self.__parse(verbose)

    def add_coverage(self, offset: int, length: int, unique: bool = True) -> None:
        for i in range(offset, offset + length):
            if self.coverage[i] and unique:
                raise Exception(f"Already covered {hex(offset)}!")
            self.coverage[i] = True

    def as_dict(self) -> Dict[str, Any]:
        return {
            'endian': self.endian,
            'features': self.features,
            'file_flags': "".join(_hex(x) for x in self.file_flags),
            'obfuscated': self.text_obfuscated,
            'legacy_lz': self.legacy_lz,
            'modern_lz': self.modern_lz,
            'textures': [tex.as_dict() for tex in self.textures],
            'texturemap': self.texturemap.as_dict(),
            'textureregion': [reg.as_dict() for reg in self.texture_to_region],
            'regionmap': self.regionmap.as_dict(),
            'swfdata': [data.as_dict() for data in self.swfdata],
            'swfmap': self.swfmap.as_dict(),
            'fontdata': str(self.fontdata) if self.fontdata is not None else None,
            'shapes': [shape.as_dict() for shape in self.shapes],
            'shapemap': self.shapemap.as_dict(),
            'unknown1': [unk.as_dict() for unk in self.unknown1],
            'unknown1map': self.unk_pman1.as_dict(),
            'unknown2': [unk.as_dict() for unk in self.unknown2],
            'unknown2map': self.unk_pman2.as_dict(),
        }

    def print_coverage(self) -> None:
        # First offset that is not coverd in a run.
        start = None

        for offset, covered in enumerate(self.coverage):
            if covered:
                if start is not None:
                    print(f"Uncovered: {hex(start)} - {hex(offset)} ({offset-start} bytes)", file=sys.stderr)
                    start = None
            else:
                if start is None:
                    start = offset
        if start is not None:
            # Print final range
            offset = len(self.coverage)
            print(f"Uncovered: {hex(start)} - {hex(offset)} ({offset-start} bytes)", file=sys.stderr)

    @staticmethod
    def cap32(val: int) -> int:
        return val & 0xFFFFFFFF

    @staticmethod
    def poly(val: int) -> int:
        if (val >> 31) & 1 != 0:
            return 0x4C11DB7
        else:
            return 0

    @staticmethod
    def crc32(bytestream: bytes) -> int:
        # Janky 6-bit CRC for ascii names in PMAN structures.
        result = 0
        for byte in bytestream:
            for i in range(6):
                result = AFPFile.poly(result) ^ AFPFile.cap32((result << 1) | ((byte >> i) & 1))
        return result

    @staticmethod
    def descramble_text(text: bytes, obfuscated: bool) -> str:
        if len(text):
            if obfuscated and (text[0] - 0x20) > 0x7F:
                # Gotta do a weird demangling where we swap the
                # top bit.
                return bytes(((x + 0x80) & 0xFF) for x in text).decode('ascii')
            else:
                return text.decode('ascii')
        else:
            return ""

    @staticmethod
    def scramble_text(text: str, obfuscated: bool) -> bytes:
        if obfuscated:
            return bytes(((x + 0x80) & 0xFF) for x in text.encode('ascii')) + b'\0'
        else:
            return text.encode('ascii') + b'\0'

    def get_until_null(self, offset: int) -> bytes:
        out = b""
        while self.data[offset] != 0:
            out += self.data[offset:(offset + 1)]
            offset += 1
        return out

    def descramble_pman(self, offset: int, verbose: bool) -> PMAN:
        # Suppress debug text unless asked
        if verbose:
            def vprint(*args: Any, **kwargs: Any) -> None:  # type: ignore
                print(*args, **kwargs, file=sys.stderr)

            add_coverage = self.add_coverage
        else:
            def vprint(*args: Any, **kwargs: Any) -> None:  # type: ignore
                pass

            def add_coverage(*args: Any, **kwargs: Any) -> None:  # type: ignore
                pass

        # Unclear what the first three unknowns are, but the fourth
        # looks like it could possibly be two int16s indicating unknown?
        magic, expect_zero, flags1, flags2, numentries, flags3, data_offset = struct.unpack(
            f"{self.endian}4sIIIIII",
            self.data[offset:(offset + 28)],
        )
        add_coverage(offset, 28)

        # I have never seen the first unknown be anything other than zero,
        # so lets lock that down.
        if expect_zero != 0:
            raise Exception("Got a non-zero value for expected zero location in PMAN!")

        if self.endian == "<" and magic != b"PMAN":
            raise Exception("Invalid magic value in PMAN structure!")
        if self.endian == ">" and magic != b"NAMP":
            raise Exception("Invalid magic value in PMAN structure!")

        names: List[Optional[str]] = [None] * numentries
        ordering: List[Optional[int]] = [None] * numentries
        if numentries > 0:
            # Jump to the offset, parse it out
            for i in range(numentries):
                file_offset = data_offset + (i * 12)
                name_crc, entry_no, nameoffset = struct.unpack(
                    f"{self.endian}III",
                    self.data[file_offset:(file_offset + 12)],
                )
                add_coverage(file_offset, 12)

                if nameoffset == 0:
                    raise Exception("Expected name offset in PMAN data!")

                bytedata = self.get_until_null(nameoffset)
                add_coverage(nameoffset, len(bytedata) + 1, unique=False)
                name = AFPFile.descramble_text(bytedata, self.text_obfuscated)
                names[entry_no] = name
                ordering[entry_no] = i
                vprint(f"    {entry_no}: {name}, offset: {hex(nameoffset)}")

                if name_crc != AFPFile.crc32(name.encode('ascii')):
                    raise Exception(f"Name CRC failed for {name}")

        for i, name in enumerate(names):
            if name is None:
                raise Exception(f"Didn't get mapping for entry {i + 1}")

        for i, o in enumerate(ordering):
            if o is None:
                raise Exception(f"Didn't get ordering for entry {i + 1}")

        return PMAN(
            entries=names,
            ordering=ordering,
            flags1=flags1,
            flags2=flags2,
            flags3=flags3,
        )

    def __parse(
        self,
        verbose: bool = False,
    ) -> None:
        # Suppress debug text unless asked
        if verbose:
            def vprint(*args: Any, **kwargs: Any) -> None:  # type: ignore
                print(*args, **kwargs, file=sys.stderr)

            add_coverage = self.add_coverage
        else:
            def vprint(*args: Any, **kwargs: Any) -> None:  # type: ignore
                pass

            def add_coverage(*args: Any, **kwargs: Any) -> None:  # type: ignore
                pass

        # First, check the signature
        if self.data[0:4] == b"2PXT":
            self.endian = "<"
        elif self.data[0:4] == b"TXP2":
            self.endian = ">"
        else:
            raise Exception("Invalid graphic file format!")
        add_coverage(0, 4)

        # Not sure what words 2 and 3 are, they seem to be some sort of
        # version or date?
        self.file_flags = self.data[4:12]
        add_coverage(4, 8)

        # Now, grab the file length, verify that we have the right amount
        # of data.
        length = struct.unpack(f"{self.endian}I", self.data[12:16])[0]
        add_coverage(12, 4)
        if length != len(self.data):
            raise Exception(f"Invalid graphic file length, expecting {length} bytes!")

        # This is always the header length, or the offset of the data payload.
        header_length = struct.unpack(f"{self.endian}I", self.data[16:20])[0]
        add_coverage(16, 4)

        # Now, the meat of the file format. Bytes 20-24 are a bitfield for
        # what parts of the header exist in the file. We need to understand
        # each bit so we know how to skip past each section.
        feature_mask = struct.unpack(f"{self.endian}I", self.data[20:24])[0]
        add_coverage(20, 4)
        header_offset = 24

        # Lots of magic happens if this bit is set.
        self.text_obfuscated = bool(feature_mask & 0x20)
        self.legacy_lz = bool(feature_mask & 0x04)
        self.modern_lz = bool(feature_mask & 0x40000)
        self.features = feature_mask

        if feature_mask & 0x01:
            # List of textures that exist in the file, with pointers to their data.
            length, offset = struct.unpack(f"{self.endian}II", self.data[header_offset:(header_offset + 8)])
            add_coverage(header_offset, 8)
            header_offset += 8

            vprint(f"Bit 0x000001 - textures; count: {length}, offset: {hex(offset)}")

            for x in range(length):
                interesting_offset = offset + (x * 12)
                if interesting_offset != 0:
                    name_offset, texture_length, texture_offset = struct.unpack(
                        f"{self.endian}III",
                        self.data[interesting_offset:(interesting_offset + 12)],
                    )
                    add_coverage(interesting_offset, 12)

                    if name_offset != 0:
                        # Let's decode this until the first null.
                        bytedata = self.get_until_null(name_offset)
                        add_coverage(name_offset, len(bytedata) + 1, unique=False)
                        name = AFPFile.descramble_text(bytedata, self.text_obfuscated)

                    if name_offset != 0 and texture_offset != 0:
                        if self.legacy_lz:
                            raise Exception("We don't support legacy lz mode!")
                        elif self.modern_lz:
                            # Get size, round up to nearest power of 4
                            inflated_size, deflated_size = struct.unpack(
                                ">II",
                                self.data[texture_offset:(texture_offset + 8)],
                            )
                            add_coverage(texture_offset, 8)
                            if deflated_size != (texture_length - 8):
                                raise Exception("We got an incorrect length for lz texture!")
                            vprint(f"    {name}, length: {texture_length}, offset: {hex(texture_offset)}, deflated_size: {deflated_size}, inflated_size: {inflated_size}")
                            inflated_size = (inflated_size + 3) & (~3)

                            # Get the data offset.
                            lz_data_offset = texture_offset + 8
                            lz_data = self.data[lz_data_offset:(lz_data_offset + deflated_size)]
                            add_coverage(lz_data_offset, deflated_size)

                            # This takes forever, so skip it if we're pretending.
                            lz77 = Lz77()
                            raw_data = lz77.decompress(lz_data)
                        else:
                            inflated_size, deflated_size = struct.unpack(
                                ">II",
                                self.data[texture_offset:(texture_offset + 8)],
                            )

                            # I'm guessing how raw textures work because I haven't seen them.
                            # I assume they're like the above, so lets put in some asertions.
                            if deflated_size != (texture_length - 8):
                                raise Exception("We got an incorrect length for raw texture!")
                            vprint(f"    {name}, length: {texture_length}, offset: {hex(texture_offset)}, deflated_size: {deflated_size}, inflated_size: {inflated_size}")

                            # Just grab the raw data.
                            lz_data = None
                            raw_data = self.data[(texture_offset + 8):(texture_offset + 8 + deflated_size)]
                            add_coverage(texture_offset, deflated_size + 8)

                        (
                            magic,
                            header_flags1,
                            header_flags2,
                            raw_length,
                            width,
                            height,
                            fmtflags,
                            expected_zero1,
                            expected_zero2,
                        ) = struct.unpack(
                            f"{self.endian}4sIIIHHIII",
                            raw_data[0:32],
                        )
                        if raw_length != len(raw_data):
                            raise Exception("Invalid texture length!")
                        # I have only ever observed the following values across two different games.
                        # Don't want to keep the chunk around so let's assert our assumptions.
                        if (expected_zero1 | expected_zero2) != 0:
                            raise Exception("Found unexpected non-zero value in texture header!")
                        if raw_data[32:44] != b'\0' * 12:
                            raise Exception("Found unexpected non-zero value in texture header!")
                        # This is almost ALWAYS 3, but I've seen it be 1 as well, so I guess we have to
                        # round-trip it if we want to write files back out. I have no clue what it's for.
                        # I've seen it be 1 only on files used for fonts so far, but I am not sure there
                        # is any correlation there.
                        header_flags3 = struct.unpack(f"{self.endian}I", raw_data[44:48])[0]
                        if raw_data[48:64] != b'\0' * 16:
                            raise Exception("Found unexpected non-zero value in texture header!")
                        fmt = fmtflags & 0xFF

                        # Extract flags that the game cares about.
                        # flags1 = (fmtflags >> 24) & 0xFF
                        # flags2 = (fmtflags >> 16) & 0xFF

                        # unk1 = 3 if (flags1 & 0xF == 1) else 1
                        # unk2 = 3 if ((flags1 >> 4) & 0xF == 1) else 1
                        # unk3 = 1 if (flags2 & 0xF == 1) else 2
                        # unk4 = 1 if ((flags2 >> 4) & 0xF == 1) else 2

                        if self.endian == "<" and magic != b"TDXT":
                            raise Exception("Unexpected texture format!")
                        if self.endian == ">" and magic != b"TXDT":
                            raise Exception("Unexpected texture format!")

                        # Since the AFP file format can be found in both big and little endian, its
                        # possible that some of these loaders might need byteswapping on some platforms.
                        # This has been tested on files intended for X86 (little endian).

                        if fmt == 0x0B:
                            # 16-bit 565 color RGB format. Game references D3D9 texture format 23 (R5G6B5).
                            newdata = []
                            for i in range(width * height):
                                pixel = struct.unpack(
                                    f"{self.endian}H",
                                    raw_data[(64 + (i * 2)):(66 + (i * 2))],
                                )[0]

                                # Extract the raw values
                                red = ((pixel >> 0) & 0x1F) << 3
                                green = ((pixel >> 5) & 0x3F) << 2
                                blue = ((pixel >> 11) & 0x1F) << 3

                                # Scale the colors so they fill the entire 8 bit range.
                                red = red | (red >> 5)
                                green = green | (green >> 6)
                                blue = blue | (blue >> 5)

                                newdata.append(
                                    struct.pack("<BBB", blue, green, red)
                                )
                            img = Image.frombytes(
                                'RGB', (width, height), b''.join(newdata), 'raw', 'RGB',
                            )
                        elif fmt == 0x0E:
                            # RGB image, no alpha. Game references D3D9 texture format 22 (R8G8B8).
                            img = Image.frombytes(
                                'RGB', (width, height), raw_data[64:], 'raw', 'RGB',
                            )
                        elif fmt == 0x10:
                            # Seems to be some sort of RGB with color swapping. Game references D3D9 texture
                            # format 21 (A8R8B8G8) but does manual byteswapping.
                            # TODO: Not sure this is correct, need to find sample files.
                            img = Image.frombytes(
                                'RGB', (width, height), raw_data[64:], 'raw', 'BGR',
                            )
                        elif fmt == 0x13:
                            # Some 16-bit texture format. Game references D3D9 texture format 25 (A1R5G5B5).
                            newdata = []
                            for i in range(width * height):
                                pixel = struct.unpack(
                                    f"{self.endian}H",
                                    raw_data[(64 + (i * 2)):(66 + (i * 2))],
                                )[0]

                                # Extract the raw values
                                alpha = 255 if ((pixel >> 15) & 0x1) != 0 else 0
                                red = ((pixel >> 0) & 0x1F) << 3
                                green = ((pixel >> 5) & 0x1F) << 3
                                blue = ((pixel >> 10) & 0x1F) << 3

                                # Scale the colors so they fill the entire 8 bit range.
                                red = red | (red >> 5)
                                green = green | (green >> 5)
                                blue = blue | (blue >> 5)

                                newdata.append(
                                    struct.pack("<BBBB", blue, green, red, alpha)
                                )
                            img = Image.frombytes(
                                'RGBA', (width, height), b''.join(newdata), 'raw', 'RGBA',
                            )
                        elif fmt == 0x15:
                            # RGBA format. Game references D3D9 texture format 21 (A8R8G8B8).
                            # Looks like unlike 0x20 below, the game does some endianness swapping.
                            # TODO: Not sure this is correct, need to find sample files.
                            img = Image.frombytes(
                                'RGBA', (width, height), raw_data[64:], 'raw', 'ARGB',
                            )
                        elif fmt == 0x16:
                            # DXT1 format. Game references D3D9 DXT1 texture format.
                            # Konami seems to have screwed up with DDR PS3 where they
                            # swap every other byte in the format, even though its specified
                            # as little-endian by all DXT1 documentation.
                            dxt = DXTBuffer(width, height)
                            img = Image.frombuffer(
                                'RGBA',
                                (width, height),
                                dxt.DXT1Decompress(raw_data[64:], swap=self.endian != "<"),
                                'raw',
                                'RGBA',
                                0,
                                1,
                            )
                        elif fmt == 0x1A:
                            # DXT5 format. Game references D3D9 DXT5 texture format.
                            # Konami seems to have screwed up with DDR PS3 where they
                            # swap every other byte in the format, even though its specified
                            # as little-endian by all DXT5 documentation.
                            dxt = DXTBuffer(width, height)
                            img = Image.frombuffer(
                                'RGBA',
                                (width, height),
                                dxt.DXT5Decompress(raw_data[64:], swap=self.endian != "<"),
                                'raw',
                                'RGBA',
                                0,
                                1,
                            )
                        elif fmt == 0x1E:
                            # I have no idea what format this is. The game does some byte
                            # swapping but doesn't actually call any texture create calls.
                            # This might be leftover from another game.
                            pass
                        elif fmt == 0x1F:
                            # 16-bit 4-4-4-4 RGBA format. Game references D3D9 texture format 26 (A4R4G4B4).
                            newdata = []
                            for i in range(width * height):
                                pixel = struct.unpack(
                                    f"{self.endian}H",
                                    raw_data[(64 + (i * 2)):(66 + (i * 2))],
                                )[0]

                                # Extract the raw values
                                blue = ((pixel >> 0) & 0xF) << 4
                                green = ((pixel >> 4) & 0xF) << 4
                                red = ((pixel >> 8) & 0xF) << 4
                                alpha = ((pixel >> 12) & 0xF) << 4

                                # Scale the colors so they fill the entire 8 bit range.
                                red = red | (red >> 4)
                                green = green | (green >> 4)
                                blue = blue | (blue >> 4)
                                alpha = alpha | (alpha >> 4)

                                newdata.append(
                                    struct.pack("<BBBB", red, green, blue, alpha)
                                )
                            img = Image.frombytes(
                                'RGBA', (width, height), b''.join(newdata), 'raw', 'RGBA',
                            )
                        elif fmt == 0x20:
                            # RGBA format. Game references D3D9 surface format 21 (A8R8G8B8).
                            img = Image.frombytes(
                                'RGBA', (width, height), raw_data[64:], 'raw', 'BGRA',
                            )
                        else:
                            vprint(f"Unsupported format {hex(fmt)} for texture {name}")
                            img = None

                        self.textures.append(
                            Texture(
                                name,
                                width,
                                height,
                                fmt,
                                header_flags1,
                                header_flags2,
                                header_flags3,
                                fmtflags & 0xFFFFFF00,
                                raw_data[64:],
                                lz_data,
                                img,
                            )
                        )
        else:
            vprint("Bit 0x000001 - textures; NOT PRESENT")

        # Mapping between texture index and the name of the texture.
        if feature_mask & 0x02:
            # Mapping of texture name to texture index. This is used by regions to look up textures.
            offset = struct.unpack(f"{self.endian}I", self.data[header_offset:(header_offset + 4)])[0]
            add_coverage(header_offset, 4)
            header_offset += 4

            vprint(f"Bit 0x000002 - texturemapping; offset: {hex(offset)}")

            if offset != 0:
                self.texturemap = self.descramble_pman(offset, verbose)
        else:
            vprint("Bit 0x000002 - texturemapping; NOT PRESENT")

        if feature_mask & 0x04:
            vprint("Bit 0x000004 - legacy lz mode on")
        else:
            vprint("Bit 0x000004 - legacy lz mode off")

        # Mapping between region index and the texture it goes to as well as the
        # region of texture that this particular graphic makes up.
        if feature_mask & 0x08:
            # Mapping between individual graphics and their respective textures.
            # This is 10 bytes per entry. Seems to need both 0x2 (texture index)
            # and 0x10 (region index).
            length, offset = struct.unpack(f"{self.endian}II", self.data[header_offset:(header_offset + 8)])
            add_coverage(header_offset, 8)
            header_offset += 8

            vprint(f"Bit 0x000008 - regions; count: {length}, offset: {hex(offset)}")

            if offset != 0 and length > 0:
                for i in range(length):
                    descriptor_offset = offset + (10 * i)
                    texture_no, left, top, right, bottom = struct.unpack(
                        f"{self.endian}HHHHH",
                        self.data[descriptor_offset:(descriptor_offset + 10)],
                    )
                    add_coverage(descriptor_offset, 10)

                    if texture_no < 0 or texture_no >= len(self.texturemap.entries):
                        raise Exception(f"Out of bounds texture {texture_no}")

                    # Texture regions are multiplied by a power of 2. Not sure why, but the games I
                    # looked at hardcode a divide by 2 when loading regions.
                    region = TextureRegion(texture_no, left, top, right, bottom)
                    self.texture_to_region.append(region)

                    vprint(f"    {region}, offset: {hex(descriptor_offset)}")
        else:
            vprint("Bit 0x000008 - regions; NOT PRESENT")

        if feature_mask & 0x10:
            # Names of the graphics regions, so we can look into the texture_to_region
            # mapping above. Used by shapes to find the right region offset given a name.
            offset = struct.unpack(f"{self.endian}I", self.data[header_offset:(header_offset + 4)])[0]
            add_coverage(header_offset, 4)
            header_offset += 4

            vprint(f"Bit 0x000010 - regionmapping; offset: {hex(offset)}")

            if offset != 0:
                self.regionmap = self.descramble_pman(offset, verbose)
        else:
            vprint("Bit 0x000010 - regionmapping; NOT PRESENT")

        if feature_mask & 0x20:
            vprint("Bit 0x000020 - text obfuscation on")
        else:
            vprint("Bit 0x000020 - text obfuscation off")

        if feature_mask & 0x40:
            # Two unknown bytes, first is a length or a count. Secound is
            # an optional offset to grab another set of bytes from.
            length, offset = struct.unpack(f"{self.endian}II", self.data[header_offset:(header_offset + 8)])
            add_coverage(header_offset, 8)
            header_offset += 8

            vprint(f"Bit 0x000040 - unknown; count: {length}, offset: {hex(offset)}")

            if offset != 0 and length > 0:
                for i in range(length):
                    unk_offset = offset + (i * 16)
                    name_offset = struct.unpack(f"{self.endian}I", self.data[unk_offset:(unk_offset + 4)])[0]
                    add_coverage(unk_offset, 4)

                    # The game does some very bizarre bit-shifting. Its clear tha the first value
                    # points at a name structure, but its not in the correct endianness. This replicates
                    # the weird logic seen in game disassembly.
                    name_offset = (((name_offset >> 7) & 0x1FF) << 16) + ((name_offset >> 16) & 0xFFFF)
                    if name_offset != 0:
                        # Let's decode this until the first null.
                        bytedata = self.get_until_null(name_offset)
                        add_coverage(name_offset, len(bytedata) + 1, unique=False)
                        name = AFPFile.descramble_text(bytedata, self.text_obfuscated)
                        vprint(f"    {name}")

                    self.unknown1.append(
                        Unknown1(
                            name=name,
                            data=self.data[(unk_offset + 4):(unk_offset + 16)],
                        )
                    )
                    add_coverage(unk_offset + 4, 12)
        else:
            vprint("Bit 0x000040 - unknown; NOT PRESENT")

        if feature_mask & 0x80:
            # One unknown byte, treated as an offset. This is clearly the mapping for the parsed
            # structures from 0x40, but I don't know what those are.
            offset = struct.unpack(f"{self.endian}I", self.data[header_offset:(header_offset + 4)])[0]
            add_coverage(header_offset, 4)
            header_offset += 4

            vprint(f"Bit 0x000080 - unknownmapping; offset: {hex(offset)}")

            # TODO: I have no idea what this is for.
            if offset != 0:
                self.unk_pman1 = self.descramble_pman(offset, verbose)
        else:
            vprint("Bit 0x000080 - unknownmapping; NOT PRESENT")

        if feature_mask & 0x100:
            # Two unknown bytes, first is a length or a count. Secound is
            # an optional offset to grab another set of bytes from.
            length, offset = struct.unpack(f"{self.endian}II", self.data[header_offset:(header_offset + 8)])
            add_coverage(header_offset, 8)
            header_offset += 8

            vprint(f"Bit 0x000100 - unknown; count: {length}, offset: {hex(offset)}")

            if offset != 0 and length > 0:
                for i in range(length):
                    unk_offset = offset + (i * 4)
                    self.unknown2.append(
                        Unknown2(self.data[unk_offset:(unk_offset + 4)])
                    )
                    add_coverage(unk_offset, 4)
        else:
            vprint("Bit 0x000100 - unknown; NOT PRESENT")

        if feature_mask & 0x200:
            # One unknown byte, treated as an offset. Almost positive its a string mapping
            # for the above 0x100 structure. That's how this file format appears to work.
            offset = struct.unpack(f"{self.endian}I", self.data[header_offset:(header_offset + 4)])[0]
            add_coverage(header_offset, 4)
            header_offset += 4

            vprint(f"Bit 0x000200 - unknownmapping; offset: {hex(offset)}")

            # TODO: I have no idea what this is for.
            if offset != 0:
                self.unk_pman2 = self.descramble_pman(offset, verbose)
        else:
            vprint("Bit 0x000200 - unknownmapping; NOT PRESENT")

        if feature_mask & 0x400:
            # One unknown byte, treated as an offset. I have no idea what this is used for,
            # it seems to be empty data in files that I've looked at, it doesn't go to any
            # structure or mapping.
            offset = struct.unpack(f"{self.endian}I", self.data[header_offset:(header_offset + 4)])[0]
            add_coverage(header_offset, 4)
            header_offset += 4

            vprint(f"Bit 0x000400 - unknown; offset: {hex(offset)}")
        else:
            vprint("Bit 0x000400 - unknown; NOT PRESENT")

        if feature_mask & 0x800:
            # SWF raw data that is loaded and passed to AFP core. It is equivalent to the
            # afp files in an IFS container.
            length, offset = struct.unpack(f"{self.endian}II", self.data[header_offset:(header_offset + 8)])
            add_coverage(header_offset, 8)
            header_offset += 8

            vprint(f"Bit 0x000800 - swfdata; count: {length}, offset: {hex(offset)}")

            for x in range(length):
                interesting_offset = offset + (x * 12)
                if interesting_offset != 0:
                    name_offset, swf_length, swf_offset = struct.unpack(
                        f"{self.endian}III",
                        self.data[interesting_offset:(interesting_offset + 12)],
                    )
                    add_coverage(interesting_offset, 12)
                    if name_offset != 0:
                        # Let's decode this until the first null.
                        bytedata = self.get_until_null(name_offset)
                        add_coverage(name_offset, len(bytedata) + 1, unique=False)
                        name = AFPFile.descramble_text(bytedata, self.text_obfuscated)
                        vprint(f"    {name}, length: {swf_length}, offset: {hex(swf_offset)}")

                    if swf_offset != 0:
                        self.swfdata.append(
                            SWF(
                                name,
                                self.data[swf_offset:(swf_offset + swf_length)]
                            )
                        )
                        add_coverage(swf_offset, swf_length)
        else:
            vprint("Bit 0x000800 - swfdata; NOT PRESENT")

        if feature_mask & 0x1000:
            # A mapping structure that allows looking up SWF data by name.
            offset = struct.unpack(f"{self.endian}I", self.data[header_offset:(header_offset + 4)])[0]
            add_coverage(header_offset, 4)
            header_offset += 4

            vprint(f"Bit 0x001000 - swfmapping; offset: {hex(offset)}")

            if offset != 0:
                self.swfmap = self.descramble_pman(offset, verbose)
        else:
            vprint("Bit 0x001000 - swfmapping; NOT PRESENT")

        if feature_mask & 0x2000:
            # These are shapes as used with the SWF data above. They contain mappings between a
            # loaded texture shape and the region that contains data. They are equivalent to the
            # geo files found in an IFS container.
            length, offset = struct.unpack(f"{self.endian}II", self.data[header_offset:(header_offset + 8)])
            add_coverage(header_offset, 8)
            header_offset += 8

            vprint(f"Bit 0x002000 - shapes; count: {length}, offset: {hex(offset)}")

            for x in range(length):
                shape_base_offset = offset + (x * 12)
                if shape_base_offset != 0:
                    name_offset, shape_length, shape_offset = struct.unpack(
                        f"{self.endian}III",
                        self.data[shape_base_offset:(shape_base_offset + 12)],
                    )
                    add_coverage(shape_base_offset, 12)

                    if name_offset != 0:
                        # Let's decode this until the first null.
                        bytedata = self.get_until_null(name_offset)
                        add_coverage(name_offset, len(bytedata) + 1, unique=False)
                        name = AFPFile.descramble_text(bytedata, self.text_obfuscated)
                    else:
                        name = "<unnamed>"

                    if shape_offset != 0:
                        shape = Shape(
                            name,
                            self.data[shape_offset:(shape_offset + shape_length)],
                        )
                        shape.parse(text_obfuscated=self.text_obfuscated)
                        self.shapes.append(shape)
                        add_coverage(shape_offset, shape_length)

                        vprint(f"    {name}, length: {shape_length}, offset: {hex(shape_offset)}")
                        for line in str(shape).split(os.linesep):
                            vprint(f"        {line}")

        else:
            vprint("Bit 0x002000 - shapes; NOT PRESENT")

        if feature_mask & 0x4000:
            # Mapping so that shapes can be looked up by name to get their offset.
            offset = struct.unpack(f"{self.endian}I", self.data[header_offset:(header_offset + 4)])[0]
            add_coverage(header_offset, 4)
            header_offset += 4

            vprint(f"Bit 0x004000 - shapesmapping; offset: {hex(offset)}")

            if offset != 0:
                self.shapemap = self.descramble_pman(offset, verbose)
        else:
            vprint("Bit 0x004000 - shapesmapping; NOT PRESENT")

        if feature_mask & 0x8000:
            # One unknown byte, treated as an offset. I have no idea what this is because
            # the games I've looked at don't include this bit.
            offset = struct.unpack(f"{self.endian}I", self.data[header_offset:(header_offset + 4)])[0]
            add_coverage(header_offset, 4)
            header_offset += 4

            vprint(f"Bit 0x008000 - unknown; offset: {hex(offset)}")

            # Since I've never seen this, I'm going to assume that it showing up is
            # bad and make things read only.
            self.read_only = True
        else:
            vprint("Bit 0x008000 - unknown; NOT PRESENT")

        if feature_mask & 0x10000:
            # Included font package, BINXRPC encoded. This is basically a texture sheet with an XML
            # pointing at the region in the texture sheet for every renderable character.
            offset = struct.unpack(f"{self.endian}I", self.data[header_offset:(header_offset + 4)])[0]
            add_coverage(header_offset, 4)
            header_offset += 4

            # I am not sure what the unknown byte is for. It always appears as
            # all zeros in all files I've looked at.
            expect_zero, length, binxrpc_offset = struct.unpack(f"{self.endian}III", self.data[offset:(offset + 12)])
            add_coverage(offset, 12)

            vprint(f"Bit 0x010000 - fontinfo; offset: {hex(offset)}, binxrpc offset: {hex(binxrpc_offset)}")

            if expect_zero != 0:
                # If we find non-zero versions of this, then that means updating the file is
                # potentially unsafe as we could rewrite it incorrectly. So, let's assert!
                raise Exception("Expected a zero in font package header!")

            if binxrpc_offset != 0:
                self.fontdata = self.benc.decode(self.data[binxrpc_offset:(binxrpc_offset + length)])
                add_coverage(binxrpc_offset, length)
            else:
                self.fontdata = None
        else:
            vprint("Bit 0x010000 - fontinfo; NOT PRESENT")

        if feature_mask & 0x20000:
            # This is the byteswapping headers that allow us to byteswap the SWF data before passing it
            # to AFP core. It is equivalent to the bsi files in an IFS container.
            offset = struct.unpack(f"{self.endian}I", self.data[header_offset:(header_offset + 4)])[0]
            add_coverage(header_offset, 4)
            header_offset += 4

            vprint(f"Bit 0x020000 - swfheaders; offset: {hex(offset)}")

            if offset > 0 and len(self.swfdata) > 0:
                for i in range(len(self.swfdata)):
                    structure_offset = offset + (i * 12)

                    # First word is always zero, as observed. I am not ENTIRELY sure that
                    # the second field is length, but it lines up with everything else
                    # I've observed and seems to make sense.
                    expect_zero, afp_header_length, afp_header = struct.unpack(
                        f"{self.endian}III",
                        self.data[structure_offset:(structure_offset + 12)]
                    )
                    vprint(f"    length: {afp_header_length}, offset: {hex(afp_header)}")
                    add_coverage(structure_offset, 12)

                    if expect_zero != 0:
                        # If we find non-zero versions of this, then that means updating the file is
                        # potentially unsafe as we could rewrite it incorrectly. So, let's assert!
                        raise Exception("Expected a zero in SWF header!")

                    self.swfdata[i].descramble_info = self.data[afp_header:(afp_header + afp_header_length)]
                    add_coverage(afp_header, afp_header_length)
        else:
            vprint("Bit 0x020000 - swfheaders; NOT PRESENT")

        if feature_mask & 0x40000:
            vprint("Bit 0x040000 - modern lz mode on")
        else:
            vprint("Bit 0x040000 - modern lz mode off")

        if feature_mask & 0xFFF80000:
            # We don't know these bits at all!
            raise Exception("Invalid bits set in feature mask!")

        if header_offset != header_length:
            raise Exception("Failed to parse bitfield of header correctly!")
        if verbose:
            self.print_coverage()

        # Now, parse out the SWF data in each of the SWF structures we found.
        for swf in self.swfdata:
            swf.parse(verbose)

    @staticmethod
    def align(val: int) -> int:
        return (val + 3) & 0xFFFFFFFFC

    @staticmethod
    def pad(data: bytes, length: int) -> bytes:
        if len(data) == length:
            return data
        elif len(data) > length:
            raise Exception("Logic error, padding request in data already written!")
        return data + (b"\0" * (length - len(data)))

    def write_strings(self, data: bytes, strings: Dict[str, int]) -> bytes:
        tuples: List[Tuple[str, int]] = [(name, strings[name]) for name in strings]
        tuples = sorted(tuples, key=lambda tup: tup[1])

        for (string, offset) in tuples:
            data = AFPFile.pad(data, offset)
            data += AFPFile.scramble_text(string, self.text_obfuscated)

        return data

    def write_pman(self, data: bytes, offset: int, pman: PMAN, string_offsets: Dict[str, int]) -> bytes:
        # First, lay down the PMAN header
        if self.endian == "<":
            magic = b"PMAN"
        elif self.endian == ">":
            magic = b"NAMP"
        else:
            raise Exception("Logic error, unexpected endianness!")

        # Calculate where various data goes
        data = AFPFile.pad(data, offset)
        payload_offset = offset + 28
        string_offset = payload_offset + (len(pman.entries) * 12)
        pending_strings: Dict[str, int] = {}

        data += struct.pack(
            f"{self.endian}4sIIIIII",
            magic,
            0,
            pman.flags1,
            pman.flags2,
            len(pman.entries),
            pman.flags3,
            payload_offset,
        )

        # Now, lay down the individual entries
        datas: List[bytes] = [b""] * len(pman.entries)
        for entry_no, name in enumerate(pman.entries):
            name_crc = AFPFile.crc32(name.encode('ascii'))

            if name not in string_offsets:
                # We haven't written this string out yet, so put it on our pending list.
                pending_strings[name] = string_offset
                string_offsets[name] = string_offset

                # Room for the null byte!
                string_offset += len(name) + 1

            # Write out the chunk itself.
            datas[pman.ordering[entry_no]] = struct.pack(
                f"{self.endian}III",
                name_crc,
                entry_no,
                string_offsets[name],
            )

        # Write it out in the correct order. Some files are hardcoded in various
        # games so we MUST preserve the order of PMAN entries.
        data += b"".join(datas)

        # Now, put down the strings that were new in this pman structure.
        return self.write_strings(data, pending_strings)

    def unparse(self) -> bytes:
        if self.read_only:
            raise Exception("This file is read-only because we can't parse some of it!")

        # Mapping from various strings found in the file to their offsets.
        string_offsets: Dict[str, int] = {}
        pending_strings: Dict[str, int] = {}

        # The true file header, containing magic, some file flags, file length and
        # header length.
        header: bytes = b''

        # The bitfield structure that dictates what's found in the file and where.
        bitfields: bytes = b''

        # The data itself.
        body: bytes = b''

        # First, plop down the file magic as well as the unknown file flags we
        # roundtripped.
        if self.endian == "<":
            header += b"2PXT"
        elif self.endian == ">":
            header += b"TXP2"
        else:
            raise Exception("Invalid graphic file format!")

        # Not sure what words 2 and 3 are, they seem to be some sort of
        # version or date?
        header += self.data[4:12]

        # We can't plop the length down yet, since we don't know it. So, let's first
        # figure out what our bitfield length is.
        header_length = 0
        if self.features & 0x1:
            header_length += 8
        if self.features & 0x2:
            header_length += 4
        # Bit 0x4 is for lz options.
        if self.features & 0x8:
            header_length += 8
        if self.features & 0x10:
            header_length += 4
        # Bit 0x20 is for text obfuscation options.
        if self.features & 0x40:
            header_length += 8
        if self.features & 0x80:
            header_length += 4
        if self.features & 0x100:
            header_length += 8
        if self.features & 0x200:
            header_length += 4
        if self.features & 0x400:
            header_length += 4
        if self.features & 0x800:
            header_length += 8
        if self.features & 0x1000:
            header_length += 4
        if self.features & 0x2000:
            header_length += 8
        if self.features & 0x4000:
            header_length += 4
        if self.features & 0x8000:
            header_length += 4
        if self.features & 0x10000:
            header_length += 4
        if self.features & 0x20000:
            header_length += 4
        # Bit 0x40000 is for lz options.

        # We keep this indirection because we want to do our best to preserve
        # the file order we observe in actual files. So, that means writing data
        # out of order of when it shows in the header, and as such we must remember
        # what chunks go where. We key by feature bitmask so its safe to have empties.
        bitchunks = [b""] * 32

        # Pad out the body for easier calculations below
        body = AFPFile.pad(body, 24 + header_length)

        # Start laying down various file pieces.
        texture_to_update_offset: Dict[str, Tuple[int, bytes]] = {}
        if self.features & 0x01:
            # List of textures that exist in the file, with pointers to their data.
            offset = AFPFile.align(len(body))
            body = AFPFile.pad(body, offset)

            # First, lay down pointers and length, regardless of number of entries.
            bitchunks[0] = struct.pack(f"{self.endian}II", len(self.textures), offset)

            # Now, calculate how long each texture is and formulate the data itself.
            name_to_length: Dict[str, int] = {}

            # Now, possibly compress and lay down textures.
            for texture in self.textures:
                # Construct the TXDT texture format from our parsed results.
                if self.endian == "<":
                    magic = b"TDXT"
                elif self.endian == ">":
                    magic != b"TXDT"
                else:
                    raise Exception("Unexpected texture format!")

                fmtflags = (texture.fmtflags & 0xFFFFFF00) | (texture.fmt & 0xFF)

                raw_texture = struct.pack(
                    f"{self.endian}4sIIIHHIII",
                    magic,
                    texture.header_flags1,
                    texture.header_flags2,
                    64 + len(texture.raw),
                    texture.width,
                    texture.height,
                    fmtflags,
                    0,
                    0,
                ) + (b'\0' * 12) + struct.pack(
                    f"{self.endian}I", texture.header_flags3,
                ) + (b'\0' * 16) + texture.raw

                if self.legacy_lz:
                    raise Exception("We don't support legacy lz mode!")
                elif self.modern_lz:
                    if texture.compressed:
                        # We didn't change this texture, use the original compression.
                        compressed_texture = texture.compressed
                    else:
                        # We need to compress the raw texture.
                        lz77 = Lz77()
                        compressed_texture = lz77.compress(raw_texture)

                    # Construct the mini-header and the texture itself.
                    name_to_length[texture.name] = len(compressed_texture) + 8
                    texture_to_update_offset[texture.name] = (
                        0xDEADBEEF,
                        struct.pack(
                            ">II",
                            len(raw_texture),
                            len(compressed_texture),
                        ) + compressed_texture,
                    )
                else:
                    # We just need to place the raw texture down.
                    name_to_length[texture.name] = len(raw_texture) + 8
                    texture_to_update_offset[texture.name] = (
                        0xDEADBEEF,
                        struct.pack(
                            ">II",
                            len(raw_texture),
                            len(raw_texture),
                        ) + raw_texture,
                    )

            # Now, make sure the texture block is padded to 4 bytes, so we can figure out
            # where strings go.
            string_offset = AFPFile.align(len(body) + (len(self.textures) * 12))

            # Now, write out texture pointers and strings.
            for texture in self.textures:
                if texture.name not in string_offsets:
                    # We haven't written this string out yet, so put it on our pending list.
                    pending_strings[texture.name] = string_offset
                    string_offsets[texture.name] = string_offset

                    # Room for the null byte!
                    string_offset += len(texture.name) + 1

                # Write out the chunk itself, remember where we need to fix up later.
                texture_to_update_offset[texture.name] = (
                    len(body) + 8,
                    texture_to_update_offset[texture.name][1],
                )
                body += struct.pack(
                    f"{self.endian}III",
                    string_offsets[texture.name],
                    name_to_length[texture.name],  # Structure length
                    0xDEADBEEF,  # Structure offset (we will fix this later)
                )

            # Now, put down the texture chunk itself and then strings that were new in this chunk.
            body = self.write_strings(body, pending_strings)
            pending_strings = {}

        if self.features & 0x08:
            # Mapping between individual graphics and their respective textures.
            offset = AFPFile.align(len(body))
            body = AFPFile.pad(body, offset)

            # First, lay down pointers and length, regardless of number of entries.
            bitchunks[3] = struct.pack(f"{self.endian}II", len(self.texture_to_region), offset)

            for bounds in self.texture_to_region:
                body += struct.pack(
                    f"{self.endian}HHHHH",
                    bounds.textureno,
                    bounds.left,
                    bounds.top,
                    bounds.right,
                    bounds.bottom,
                )

        if self.features & 0x40:
            # Unknown file chunk.
            offset = AFPFile.align(len(body))
            body = AFPFile.pad(body, offset)

            # First, lay down pointers and length, regardless of number of entries.
            bitchunks[6] = struct.pack(f"{self.endian}II", len(self.unknown1), offset)

            # Now, calculate where we can put strings.
            string_offset = AFPFile.align(len(body) + (len(self.unknown1) * 16))

            # Now, write out chunks and strings.
            for entry1 in self.unknown1:
                if entry1.name not in string_offsets:
                    # We haven't written this string out yet, so put it on our pending list.
                    pending_strings[entry1.name] = string_offset
                    string_offsets[entry1.name] = string_offset

                    # Room for the null byte!
                    string_offset += len(entry1.name) + 1

                # Write out the chunk itself.
                body += struct.pack(f"{self.endian}I", string_offsets[entry1.name]) + entry1.data

            # Now, put down the strings that were new in this chunk.
            body = self.write_strings(body, pending_strings)
            pending_strings = {}

        if self.features & 0x100:
            # Two unknown bytes, first is a length or a count. Secound is
            # an optional offset to grab another set of bytes from.
            offset = AFPFile.align(len(body))
            body = AFPFile.pad(body, offset)

            # First, lay down pointers and length, regardless of number of entries.
            bitchunks[8] = struct.pack(f"{self.endian}II", len(self.unknown2), offset)

            # Now, write out chunks and strings.
            for entry2 in self.unknown2:
                # Write out the chunk itself.
                body += entry2.data

        if self.features & 0x800:
            # This is the names and locations of the SWF data as far as I can tell.
            offset = AFPFile.align(len(body))
            body = AFPFile.pad(body, offset)

            bitchunks[11] = struct.pack(f"{self.endian}II", len(self.swfdata), offset)

            # Now, calculate where we can put SWF data and their names.
            swfdata_offset = AFPFile.align(len(body) + (len(self.swfdata) * 12))
            string_offset = AFPFile.align(swfdata_offset + sum(AFPFile.align(len(a.data)) for a in self.swfdata))
            swfdata = b""

            # Now, lay them out.
            for data in self.swfdata:
                if data.name not in string_offsets:
                    # We haven't written this string out yet, so put it on our pending list.
                    pending_strings[data.name] = string_offset
                    string_offsets[data.name] = string_offset

                    # Room for the null byte!
                    string_offset += len(data.name) + 1

                # Write out the chunk itself.
                body += struct.pack(
                    f"{self.endian}III",
                    string_offsets[data.name],
                    len(data.data),
                    swfdata_offset + len(swfdata),
                )
                swfdata += AFPFile.pad(data.data, AFPFile.align(len(data.data)))

            # Now, lay out the data itself and finally string names.
            body = self.write_strings(body + swfdata, pending_strings)
            pending_strings = {}

        if self.features & 0x2000:
            # This is the names and data for shapes as far as I can tell.
            offset = AFPFile.align(len(body))
            body = AFPFile.pad(body, offset)

            bitchunks[13] = struct.pack(f"{self.endian}II", len(self.shapes), offset)

            # Now, calculate where we can put shapes and their names.
            shape_offset = AFPFile.align(len(body) + (len(self.shapes) * 12))
            string_offset = AFPFile.align(shape_offset + sum(AFPFile.align(len(s.data)) for s in self.shapes))
            shapedata = b""

            # Now, lay them out.
            for shape in self.shapes:
                if shape.name not in string_offsets:
                    # We haven't written this string out yet, so put it on our pending list.
                    pending_strings[shape.name] = string_offset
                    string_offsets[shape.name] = string_offset

                    # Room for the null byte!
                    string_offset += len(shape.name) + 1

                # Write out the chunk itself.
                body += struct.pack(
                    f"{self.endian}III",
                    string_offsets[shape.name],
                    len(shape.data),
                    shape_offset + len(shapedata),
                )
                shapedata += AFPFile.pad(shape.data, AFPFile.align(len(shape.data)))

            # Now, lay out the data itself and finally string names.
            body = self.write_strings(body + shapedata, pending_strings)
            pending_strings = {}

        if self.features & 0x02:
            # Mapping between texture index and the name of the texture.
            offset = AFPFile.align(len(body))
            body = AFPFile.pad(body, offset)

            # Lay down PMAN pointer and PMAN structure itself.
            bitchunks[1] = struct.pack(f"{self.endian}I", offset)
            body = self.write_pman(body, offset, self.texturemap, string_offsets)

        if self.features & 0x10:
            # Names of the graphics regions, so we can look into the texture_to_region
            # mapping above.
            offset = AFPFile.align(len(body))
            body = AFPFile.pad(body, offset)

            # Lay down PMAN pointer and PMAN structure itself.
            bitchunks[4] = struct.pack(f"{self.endian}I", offset)
            body = self.write_pman(body, offset, self.regionmap, string_offsets)

        if self.features & 0x80:
            # One unknown byte, treated as an offset. This is clearly the mapping for the parsed
            # structures from 0x40, but I don't know what those are.
            offset = AFPFile.align(len(body))
            body = AFPFile.pad(body, offset)

            # Lay down PMAN pointer and PMAN structure itself.
            bitchunks[7] = struct.pack(f"{self.endian}I", offset)
            body = self.write_pman(body, offset, self.unk_pman1, string_offsets)

        if self.features & 0x200:
            # I am pretty sure this is a mapping for the structures parsed at 0x100.
            offset = AFPFile.align(len(body))
            body = AFPFile.pad(body, offset)

            # Lay down PMAN pointer and PMAN structure itself.
            bitchunks[9] = struct.pack(f"{self.endian}I", offset)
            body = self.write_pman(body, offset, self.unk_pman2, string_offsets)

        if self.features & 0x1000:
            # Mapping of SWF data to their ID.
            offset = AFPFile.align(len(body))
            body = AFPFile.pad(body, offset)

            # Lay down PMAN pointer and PMAN structure itself.
            bitchunks[12] = struct.pack(f"{self.endian}I", offset)
            body = self.write_pman(body, offset, self.swfmap, string_offsets)

        if self.features & 0x4000:
            # Mapping of shapes to their ID.
            offset = AFPFile.align(len(body))
            body = AFPFile.pad(body, offset)

            # Lay down PMAN pointer and PMAN structure itself.
            bitchunks[14] = struct.pack(f"{self.endian}I", offset)
            body = self.write_pman(body, offset, self.shapemap, string_offsets)

        if self.features & 0x10000:
            # Font information.
            offset = AFPFile.align(len(body))
            body = AFPFile.pad(body, offset)

            bitchunks[16] = struct.pack(f"{self.endian}I", offset)

            # Now, encode the font information.
            fontbytes = self.benc.encode(self.fontdata)
            body += struct.pack(
                f"{self.endian}III",
                0,
                len(fontbytes),
                offset + 12,
            )
            body += fontbytes

        if self.features & 0x400:
            # I haven't seen any files with any meaningful information for this, but
            # it gets included anyway since games seem to parse it.
            offset = AFPFile.align(len(body))
            body = AFPFile.pad(body, offset)

            # Point to current data location (seems to be what original files do too).
            bitchunks[10] = struct.pack(f"{self.endian}I", offset)

        if self.features & 0x8000:
            # Unknown, never seen bit. We shouldn't be here, we set ourselves
            # to read-only.
            raise Exception("This should not be possible!")

        if self.features & 0x20000:
            # SWF header information.
            offset = AFPFile.align(len(body))
            body = AFPFile.pad(body, offset)

            bitchunks[17] = struct.pack(f"{self.endian}I", offset)

            # Now, calculate where we can put SWF headers.
            swfdata_offset = AFPFile.align(len(body) + (len(self.swfdata) * 12))
            swfheader = b""

            # Now, lay them out.
            for data in self.swfdata:
                # Write out the chunk itself.
                body += struct.pack(
                    f"{self.endian}III",
                    0,
                    len(data.descramble_info),
                    swfdata_offset + len(swfheader),
                )
                swfheader += AFPFile.pad(data.descramble_info, AFPFile.align(len(data.descramble_info)))

            # Now, lay out the header itself
            body += swfheader

        if self.features & 0x01:
            # Now, go back and add texture data to the end of the file, fixing up the
            # pointer to said data we wrote down earlier.
            for texture in self.textures:
                # Grab the offset we need to fix, our current offset and place
                # the texture data itself down.
                fix_offset, texture_data = texture_to_update_offset[texture.name]
                offset = AFPFile.align(len(body))
                body = AFPFile.pad(body, offset) + texture_data

                # Now, update the patch location to make sure we point at the texture data.
                body = body[:fix_offset] + struct.pack(f"{self.endian}I", offset) + body[(fix_offset + 4):]

        # Bit 0x40000 is for lz options.

        # Now, no matter what happened above, make sure file is aligned to 4 bytes.
        offset = AFPFile.align(len(body))
        body = AFPFile.pad(body, offset)

        # Record the bitfield options into the bitfield structure, and we can
        # get started writing the file out.
        bitfields = struct.pack(f"{self.endian}I", self.features) + b"".join(bitchunks)

        # Finally, now that we know the full file length, we can finish
        # writing the header.
        header += struct.pack(f"{self.endian}II", len(body), header_length + 24)
        if len(header) != 20:
            raise Exception("Logic error, incorrect header length!")

        # Skip over padding to the body that we inserted specifically to track offsets
        # against the headers.
        return header + bitfields + body[(header_length + 24):]

    def update_texture(self, name: str, png_data: bytes) -> None:
        for texture in self.textures:
            if texture.name == name:
                # First, let's get the dimensions of this new picture and
                # ensure that it is identical to the existing one.
                img = Image.open(io.BytesIO(png_data))
                if img.width != texture.width or img.height != texture.height:
                    raise Exception("Cannot update texture with different size!")

                # Now, get the raw image data.
                img = img.convert('RGBA')
                texture.img = img

                # Now, refresh the raw texture data for when we write it out.
                self._refresh_texture(texture)

                return
        else:
            raise Exception(f"There is no texture named {name}!")

    def update_sprite(self, texture: str, sprite: str, png_data: bytes) -> None:
        # First, identify the bounds where the texture lives.
        for no, name in enumerate(self.texturemap.entries):
            if name == texture:
                textureno = no
                break
        else:
            raise Exception(f"There is no texture named {texture}!")

        for no, name in enumerate(self.regionmap.entries):
            if name == sprite:
                region = self.texture_to_region[no]
                if region.textureno == textureno:
                    # We found the region associated with the sprite we want to update.
                    break
        else:
            raise Exception(f"There is no sprite named {sprite} on texture {texture}!")

        # Now, figure out if the PNG data we got is valid.
        sprite_img = Image.open(io.BytesIO(png_data))
        if sprite_img.width != ((region.right // 2) - (region.left // 2)) or sprite_img.height != ((region.bottom // 2) - (region.top // 2)):
            raise Exception("Cannot update sprite with different size!")

        # Now, copy the data over and update the raw texture.
        for tex in self.textures:
            if tex.name == texture:
                tex.img.paste(sprite_img, (region.left // 2, region.top // 2))

                # Now, refresh the texture so when we save the file its updated.
                self._refresh_texture(tex)

    def _refresh_texture(self, texture: Texture) -> None:
        if texture.fmt == 0x0B:
            # 16-bit 565 color RGB format.
            texture.raw = b"".join(
                struct.pack(
                    f"{self.endian}H",
                    (
                        (((pixel[0] >> 3) & 0x1F) << 11) |
                        (((pixel[1] >> 2) & 0x3F) << 5) |
                        ((pixel[2] >> 3) & 0x1F)
                    )
                ) for pixel in texture.img.getdata()
            )
        elif texture.fmt == 0x13:
            # 16-bit A1R5G55 texture format.
            texture.raw = b"".join(
                struct.pack(
                    f"{self.endian}H",
                    (
                        (0x8000 if pixel[3] >= 128 else 0x0000) |
                        (((pixel[0] >> 3) & 0x1F) << 10) |
                        (((pixel[1] >> 3) & 0x1F) << 5) |
                        ((pixel[2] >> 3) & 0x1F)
                    )
                ) for pixel in texture.img.getdata()
            )
        elif texture.fmt == 0x1F:
            # 16-bit 4-4-4-4 RGBA format.
            texture.raw = b"".join(
                struct.pack(
                    f"{self.endian}H",
                    (
                        ((pixel[2] >> 4) & 0xF) |
                        (((pixel[1] >> 4) & 0xF) << 4) |
                        (((pixel[0] >> 4) & 0xF) << 8) |
                        (((pixel[3] >> 4) & 0xF) << 12)
                    )
                ) for pixel in texture.img.getdata()
            )
        elif texture.fmt == 0x20:
            # 32-bit RGBA format
            texture.raw = b"".join(
                struct.pack(
                    f"{self.endian}BBBB",
                    pixel[2],
                    pixel[1],
                    pixel[0],
                    pixel[3],
                ) for pixel in texture.img.getdata()
            )
        else:
            raise Exception(f"Unsupported format {hex(texture.fmt)} for texture {texture.name}")

        # Make sure we don't use the old compressed data.
        texture.compressed = None