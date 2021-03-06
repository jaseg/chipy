#
#  Chipy -- Constructing Hardware In PYthon
#
#  Copyright (C) 2016  Clifford Wolf <clifford@clifford.at>
#
#  Permission to use, copy, modify, and/or distribute this software for any
#  purpose with or without fee is hereby granted, provided that the above
#  copyright notice and this permission notice appear in all copies.
#
#  THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
#  WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
#  MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
#  ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
#  WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
#  ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
#  OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
#


import traceback
import os.path
import threading
from contextlib import contextmanager


class TLSDefaults:
    def __init__(self, **defaults):
        # Use self.__dict__ to circumvent __setattr__ below.
        self.__dict__['_defaults'] = defaults
        self.__dict__['_tls'] = threading.local()
        # NOTE: We cannot initialize the attributes of self._tls from defaults
        # here as __init__ may be called on a different thread than __getattr__

    def __getattr__(self, name):
        # In case this is the first access to {name} on this thread and name is
        # in _defaults, initialize it in _tls.
        if name in self._defaults and not hasattr(self._tls, name):
            setattr(self._tls, name, self._defaults[name])
        return getattr(self._tls, name)

    def __setattr__(self, name, value):
        setattr(self._tls, name, value)


tls = TLSDefaults(
        ChipyModulesDict={},
        ChipyCurrentContext=None,
        ChipyElseContext=None,
        ChipyIdCounter=0)


def ResetDesign():
    if tls.ChipyCurrentContext is not None:
        raise ValueError(
                'Cannot reset design inside open context (If/Else/Switch etc. block).')
    tls.ChipyModulesDict = dict()
    tls.ChipyElseContext = None
    tls.ChipyIdCounter = 0


class ChipyError(ValueError):
    pass


def raiseOutsideContext(name):
    if tls.ChipyCurrentContext is None:
        raise ChipyError('{} called outside chipy context'.format(name))

def ChipySameModule(modules):
    raiseOutsideContext('ChipySameModule')
    mods = {tls.ChipyCurrentContext.module, *modules} - {None}

    if len(mods) != 1:
        raise ValueError('Modules are none or not the same.')

    return mods.pop()


def ChipyAutoName():
    tls.ChipyIdCounter += 1
    return "__%d" % tls.ChipyIdCounter


def ChipyCodeLoc():
    stack = traceback.extract_stack()

    for frame in reversed(stack):
        filename = os.path.basename(frame[0])
        lineno = frame[1]

        if filename != "Chipy.py":
            return "%s:%d" % (filename, lineno)

    return "Unkown location"


class ChipyContext:
    def __init__(self, newmod=None):
        self.module = newmod
        self.snippet = None

    def add_line(self, line, lvalues=None):
        if getattr(self, 'parent') is None:
            raise ValueError('Trying to add line to closed context.')
        if self.snippet is None:
            self.snippet = ChipySnippet()
            self.module.code_snippets.append(self.snippet)

        if lvalues is not None:
            self.snippet.lvalue_signals.update(lvalues)

        self.snippet.text_lines.append(self.snippet.indent_str + line)

    def add_indent(self):
        if getattr(self, 'parent') is None:
            raise ValueError('Trying to add indent to closed context.')
        self.snippet.indent_str += "  "

    def remove_indent(self):
        if getattr(self, 'parent') is None:
            raise ValueError('Trying to remove indent from closed context.')
        self.snippet.indent_str = self.snippet.indent_str[2:]

    def popctx(self):
        tls.ChipyCurrentContext = self.parent
        self.parent = None

    def pushctx(self):
        # If we'd check for "not hasattr(self, 'parent')" here instead we would
        # prevent several subsequent blocks with the same context, which we
        # need for If/Else which both happen in the same context.
        if getattr(self, 'parent', None) is not None:
            raise ValueError('Trying to enter context that is already open.')
        self.parent = tls.ChipyCurrentContext
        if self.module is None:
            self.module = self.parent.module
            self.snippet = self.parent.snippet
        tls.ChipyCurrentContext = self

    @contextmanager
    def block(self, begin, end='end'):
        self.pushctx()
        self.add_line(begin)
        self.add_indent()

        yield self

        self.remove_indent()
        self.add_line(end)
        self.popctx()

    # Context manager protocol for ChipyContext object
    def __enter__(self):
        self.pushctx()
        return self

    def __exit__(self, type, value, traceback):
        self.popctx()


class ChipySnippet:
    def __init__(self):
        self.indent_str = "    "
        self.text_lines = list()
        self.lvalue_signals = dict()


class ChipyModule:
    def __init__(self, name):
        self.name = name
        self.signals = dict()
        self.memories = dict()
        self.regactions = list()
        self.instances = list()
        self.codeloc = ChipyCodeLoc()

        self.init_snippets = list()
        self.code_snippets = list()

        if name in tls.ChipyModulesDict:
            raise ChipyError('Module name {} already in use'.format(name))
        tls.ChipyModulesDict[name] = self

    def intf(self, prefix=""):
        def callback(addport, role):
            for signame, signal in sorted(self.signals.items()):
                if (signal.inport or signal.outport) and signame.startswith(prefix):
                    output = False
                    if signal.inport and role == "parent": output = True
                    if signal.outport and role == "child": output = True
                    addport(signame[len(prefix):], signal.width, output=output)
        return callback

    def bundle(self, prefix=""):
        ret = Bundle()
        for signame, signal in sorted(self.signals.items()):
            if signame.startswith(prefix):
                ret.add(signame[len(prefix):], signal)
        return ret

    def write_verilog(self, f):
        portlist = list()
        wirelist = list()
        assignlist = list()
        instance_lines = list()

        for memname, memory in sorted(self.memories.items()):
            wirelist.append("  %sreg [%d:0] %s [0:%d]; // %s" % ("signed " if memory.signed else "", memory.width-1, memory.name, memory.depth-1, memory.codeloc))

        for signame, signal in sorted(self.signals.items()):
            if not signal.materialize:
                continue
            if signal.inport or signal.outport:
                port_type = "inout"
                if not signal.inport: port_type = "output"
                if not signal.outport: port_type = "input"
                if signal.signed: port_type = "signed " + port_type
                if signal.vlog_reg: port_type = port_type + " reg"
                if signal.width > 1:
                    portlist.append("  %s [%d:0] %s /* %s */" % (port_type, signal.width-1, signal.name, signal.codeloc))
                else:
                    portlist.append("  %s %s /* %s */" % (port_type, signal.name, signal.codeloc))
            else:
                wire_type = "wire"
                if signal.vlog_reg: wire_type = "reg"
                if signal.width > 1:
                    wirelist.append("  %s [%d:0] %s; // %s" % (wire_type, signal.width-1, signal.name, signal.codeloc))
                else:
                    wirelist.append("  %s %s; // %s" % (wire_type, signal.name, signal.codeloc))
                if signal.vlog_rvalue is not None:
                    assignlist.append("  assign %s = %s; // %s" % (signal.name, signal.vlog_rvalue, signal.codeloc))
            if signal.register:
                if not signal.gotassign:
                    raise ChipyError("Register without assignment: %s.%s" % (signal.module.name, signal.name))
                if not signal.regaction:
                    raise ChipyError("Register without synchronization element: %s.%s" % (signal.module.name, signal.name))
                if signal.width > 1:
                    wirelist.append("  reg [%d:0] %s; // %s" % (signal.width-1, signal.vlog_lvalue, signal.codeloc))
                else:
                    wirelist.append("  reg %s; // %s" % (signal.vlog_lvalue, signal.codeloc))

        for inst_name, inst_type, inst_bundle, inst_codeloc in self.instances:
            instance_lines.append("  %s %s ( // %s" % (inst_type, inst_name, inst_codeloc))
            for member_name, member_sig in inst_bundle.items():
                expr = member_sig.name if member_sig.portalias is None else member_sig.portalias
                instance_lines.append("    .%s(%s)," % (member_name, expr))
            instance_lines[-1] = instance_lines[-1][:-1]
            instance_lines.append("  );")

        print("", file=f)
        print("module %s (" % self.name, file=f)
        print(",\n".join(portlist), file=f)
        print(");", file=f)

        for line in wirelist:
            print(line, file=f)

        for line in assignlist:
            print(line, file=f)

        snippet_db = self.init_snippets + self.code_snippets
        snippet_parent = list()
        lvalue_idx = dict()

        def UnionFind_Find(idx):
            if snippet_parent[idx] != idx:
                snippet_parent[idx] = UnionFind_Find(snippet_parent[idx])
            return snippet_parent[idx]

        def UnionFind_Union(idx1, idx2):
            idx1 = UnionFind_Find(idx1)
            idx2 = UnionFind_Find(idx2)
            snippet_parent[idx1] = idx2

        for idx in range(len(snippet_db)):
            snippet_parent.append(idx)
            for lval in snippet_db[idx].lvalue_signals.keys():
                if lval not in lvalue_idx:
                    lvalue_idx[lval] = idx
                else:
                    UnionFind_Union(idx, lvalue_idx[lval])

        snippet_groups = dict()

        for idx in range(len(snippet_db)):
            grp = UnionFind_Find(idx)
            if grp not in snippet_groups:
                snippet_groups[grp] = list()
            snippet_groups[grp].append(snippet_db[idx])

        for snippets in snippet_groups.values():
            print("  always @* begin", file=f)
            for snippet in snippets:
                # print("    // -- %s --" % (" ".join([sig.name for sig in snippet.lvalue_signals.keys()])), file=f)
                for line in snippet.text_lines:
                    print(line, file=f)
            print("  end", file=f)

        for line in self.regactions:
            print(line, file=f)

        for line in instance_lines:
            print(line, file=f)

        for memory in self.memories.values():
            if memory.posedge is not None:
                print("  always @(posedge %s) begin" % memory.posedge.name, file=f)
            if memory.negedge is not None:
                print("  always @(negedge %s) begin" % memory.negedge.name, file=f)
            for line in memory.regactions:
                print("    " + line, file=f)
            print("  end", file=f)

        print("endmodule", file=f)

    def __enter__(self):
        ChipyContext(newmod=self).pushctx()

    def __exit__(self, type, value, traceback):
        tls.ChipyCurrentContext.popctx()


def ChipyUnaryOp(vlogop, a, signprop=True, logicout=False):
    a = Sig(a)

    module = ChipySameModule([a.module])
    signal = ChipySignal(module)

    signal.signed = a.signed and signprop
    if not logicout:
        signal.width = a.width

    signal.vlog_rvalue = "%s %s" % (vlogop, a.name)
    signal.deps[a.name] = a

    return signal


def ChipyBinaryOp(vlogop, a, b, signprop=True, leftwidth=False):
    a = Sig(a)
    b = Sig(b)

    module = ChipySameModule([a.module, b.module])
    signal = ChipySignal(module)

    if leftwidth:
        signal.width = a.width
        signal.signed = a.signed and signprop
    else:
        signal.width = max(a.width, b.width)
        signal.signed = a.signed and b.signed and signprop

    signal.vlog_rvalue = "%s %s %s" % (a.name, vlogop, b.name)
    signal.deps[a.name] = a
    signal.deps[b.name] = b

    return signal


def ChipyCmpOp(vlogop, a, b):
    a = Sig(a)
    b = Sig(b)

    module = ChipySameModule([a.module, b.module])
    signal = ChipySignal(module)

    signal.vlog_rvalue = "%s %s %s" % (a.name, vlogop, b.name)
    signal.deps[a.name] = a
    signal.deps[b.name] = b

    return signal


class ChipySignal:
    def __init__(self, module, name=None, const=False):
        if name is None:
            name = ChipyAutoName()

        self.name = name
        self.module = module
        self.codeloc = ChipyCodeLoc()
        self.width = 1
        self.signed = False
        self.register = False
        self.regaction = False
        self.inport = False
        self.outport = False
        self.vlog_rvalue = None
        self.vlog_lvalue = None
        self.vlog_reg = False
        self.memory = None
        self.materialize = False
        self.gotassign = False
        self.portalias = None
        self.deps = dict()

        if not const:
            if name in module.signals:
                raise ChipyError(
                        'Signal name {} already in use in module {}'.format(name, module.name))
            module.signals[name] = self

    def get_deps(self):
        deps = {self.name: self}
        for dep in self.deps.values():
            deps.update(dep.get_deps())
        return deps

    def set_materialize(self):
        if self.materialize: return
        self.materialize = True
        for dep in self.deps.values():
            dep.set_materialize()

    def __setattr__(self, name, value):
        if name == "next":
            Assign(self, value)
        else:
            super().__setattr__(name, value)

    def __getitem__(self, index):
        if self.memory is None:
            self_name = self.name
            self_deps = {self.name: self}
        else:
            self_name = self.vlog_rvalue
            self_deps = dict()

        if isinstance(index, tuple):
            index, width = index
            if isinstance(index, slice):
                raise TypeError(
                    'Trying to index signal {} with tuple containing slice'.format(self.name))
            if not isinstance(width, int):
                raise TypeError('Second tuple element in Signal index must be "width" int')

            updown = "+" if width >= 0 else "-"
            width = abs(width)

            signal = ChipySignal(self.module)
            signal.memory = self.memory
            signal.width = width
            signal.deps.update(self_deps)

            if isinstance(index, ChipySignal):
                index.set_materialize()
                index = index.name
            elif isinstance(index, int):
                index = "%d" % index
            else:
                raise TypeError(
                    'Trying to index signal with object of type {}'.format(type(index)))

            signal.vlog_rvalue = "%s[%s %c: %d]" % (self_name, index, updown, width)
            if self.vlog_lvalue is not None:
                signal.vlog_lvalue = "%s[%s %c: %d]" % (self.vlog_lvalue, index, updown, width)
            return signal

        if isinstance(index, slice):
            msb = max(index.start, index.stop)
            lsb = min(index.start, index.stop)

            signal = ChipySignal(self.module)
            signal.memory = self.memory
            signal.width = msb - lsb + 1
            signal.deps.update(self_deps)

            signal.vlog_rvalue = "%s[%d:%d]" % (self_name, msb, lsb)
            if self.vlog_lvalue is not None:
                signal.vlog_lvalue = "%s[%d:%d]" % (self.vlog_lvalue, msb, lsb)

            return signal

        if isinstance(index, ChipySignal):
            signal = ChipySignal(self.module)
            signal.memory = self.memory
            signal.width = 1
            signal.deps.update(self_deps)

            signal.vlog_rvalue = "%s[%s]" % (self_name, index.name)
            if self.vlog_lvalue is not None:
                signal.vlog_lvalue = "%s[%s]" % (self.vlog_lvalue, index.name)

            index.set_materialize()
            return signal

        if isinstance(index, int):
            signal = ChipySignal(self.module)
            signal.memory = self.memory
            signal.width = 1
            signal.deps.update(self_deps)

            signal.vlog_rvalue = "%s[%d]" % (self_name, index)
            if self.vlog_lvalue is not None:
                signal.vlog_lvalue = "%s[%d]" % (self.vlog_lvalue, index)

            return signal

        raise NotImplementedError(
                'Indexing with object of type {} not implemented'.format(type(index)))

    def __neg__(self):
        return ChipyUnaryOp("-", self)

    def __invert__(self):
        return ChipyUnaryOp("~", self)

    def __add__(self, other):
        return ChipyBinaryOp("+", self, other)

    def __radd__(self, other):
        return ChipyBinaryOp("+", other, self)

    def __sub__(self, other):
        return ChipyBinaryOp("-", self, other)

    def __rsub__(self, other):
        return ChipyBinaryOp("-", other, self)

    def __mul__(self, other):
        return ChipyBinaryOp("*", self, other)

    def __rmul__(self, other):
        return ChipyBinaryOp("*", other, self)

    def __floordiv__(self, other):
        return ChipyBinaryOp("/", self, other)

    def __rfloordiv__(self, other):
        return ChipyBinaryOp("/", other, self)

    def __mod__(self, other):
        return ChipyBinaryOp("%", self, other)

    def __rmod__(self, other):
        return ChipyBinaryOp("%", other, self)

    def __pow__(self, other):
        return ChipyBinaryOp("**", self, other)

    def __rpow__(self, other):
        return ChipyBinaryOp("**", other, self)

    def __lshift__(self, other):
        return ChipyBinaryOp("<<<", self, other, leftwidth=True)

    def __rlshift__(self, other):
        return ChipyBinaryOp("<<<", other, self, leftwidth=True)

    def __rshift__(self, other):
        return ChipyBinaryOp(">>>", self, other, leftwidth=True)

    def __rrshift__(self, other):
        return ChipyBinaryOp(">>>", other, self, leftwidth=True)

    def __and__(self, other):
        return ChipyBinaryOp("&", self, other)

    def __rand__(self, other):
        return ChipyBinaryOp("&", other, self)

    def __xor__(self, other):
        return ChipyBinaryOp("^", self, other)

    def __rxor__(self, other):
        return ChipyBinaryOp("^", other, self)

    def __or__(self, other):
        return ChipyBinaryOp("|", self, other)

    def __ror__(self, other):
        return ChipyBinaryOp("|", other, self)

    def __lt__(self, other):
        return ChipyCmpOp("<", self, other)

    def __le__(self, other):
        return ChipyCmpOp("<=", self, other)

    def __eq__(self, other):
        return ChipyCmpOp("==", self, other)

    def __ne__(self, other):
        return ChipyCmpOp("!=", self, other)

    def __gt__(self, other):
        return ChipyCmpOp(">", self, other)

    def __ge__(self, other):
        return ChipyCmpOp(">=", self, other)

    def reduce_and(self):
        return ChipyUnaryOp("&", self, signprop=False, logicout=True)

    def reduce_or(self):
        return ChipyUnaryOp("|", self, signprop=False, logicout=True)

    def reduce_xor(self):
        return ChipyUnaryOp("^", self, signprop=False, logicout=True)

    def logic(self):
        return ChipyUnaryOp("|", self, signprop=False, logicout=True)


class ChipyMemory:
    def __init__(self, module, width, depth, name=None, posedge=None, negedge=None, signed=False):
        if name is None:
            name = ChipyAutoName()

        if (posedge is None) == (negedge is None):
            raise ValueError('posedge XOR negedge must be given')

        self.name = name
        self.module = module
        self.codeloc = ChipyCodeLoc()
        self.width = width
        self.depth = depth
        self.posedge = posedge
        self.negedge = negedge
        self.signed = signed
        self.regactions = list()

        if name in module.memories:
            raise ChipyError('Memory name {} already in use'.format(name))
        module.memories[name] = self

    def __getitem__(self, index):
        index = Sig(index)
        signal = ChipySignal(self.module)
        signal.width = self.width
        signal.vlog_rvalue = "%s[%s]" % (self.name, index.name)
        signal.deps.update(index.deps)
        signal.memory = self
        return signal


class ChipyBundle:
    def __init__(self):
        self.members = dict()

    def add(self, name, member):
        self.members[name] = member

    def regs(self):
        bundle = ChipyBundle()
        for name, member in self.members.items():
            if isinstance(member, ChipyBundle):
                bundle.add(name, member.regs())
            elif member.register:
                bundle.add(name, member)
        return bundle

    def nonregs(self):
        bundle = ChipyBundle()
        for name, member in self.members.items():
            if isinstance(member, ChipyBundle):
                bundle.add(name, member.nonregs())
            elif not member.register:
                bundle.add(name, member)
        return bundle

    def keys(self):
        return self.members.keys()

    def values(self):
        return self.members.values()

    def items(self):
        return self.members.items()

    def get(self, name):
        return self.members[name]

    def __getitem__(self, index):
        bundle = Bundle()
        for key in self.keys():
            bundle.add(key, self.get(key)[index])
        return bundle

    def __setattr__(self, name, value):
        if name == "next":
            Assign(self, value)
        else:
            super().__setattr__(name, value)

    def __getattr__(self, name):
        if name.endswith("_") and name[:-1] in self.members:
            return self.members[name[:-1]]
        raise AttributeError


def Bundle(arg=None, **kwargs):
    bundle = ChipyBundle()

    if arg is not None:
        for name, member in arg.items():
            bundle.add(name, member)

    for name, member in kwargs.items():
        assert name.endswith("_")
        bundle.add(name[:-1], member)

    return bundle


def Zip(bundles, recursive=False):
    if isinstance(bundles, (list, tuple)):
        list_mode = True
        bundles_list = bundles
        bundles_keys = range(len(bundles))
    else:
        list_mode = False
        bundles_list = list(bundles.values())
        bundles_keys = bundles.keys()

    ret = dict()

    if len(bundles_list) == 0:
        return ret

    for bundle in bundles_list:
        assert bundles_list[0].members.keys() == bundle.members.keys()

    for name in bundles_list[0].members.keys():
        if list_mode:
            value = [None] * len(bundles)
        else:
            value = dict()

        for key in bundles_keys:
            value[key] = bundles[key][name]

        ret[name] = value

    return ret


def Module(name=None):
    if name is None:
        if tls.ChipyCurrentContext is None:
            raise ChipyError('Top-level modules must be named')
        return tls.ChipyCurrentContext.module
    if name in tls.ChipyModulesDict:
        return tls.ChipyModulesDict[name]
    return None


def AddModule(name):
    return ChipyModule(name)


def AddInput(name, type=1):
    raiseOutsideContext('AddInput')

    names = name.split()
    if len(names) > 1:
        return [AddInput(n, type) for n in names]
    assert len(names) == 1
    name = names[0]

    if not isinstance(type, int):
        return AddPort(name, type, "input")

    module = tls.ChipyCurrentContext.module

    signal = ChipySignal(module, name)
    signal.width = abs(type)
    signal.signed = type < 0
    signal.inport = True
    signal.set_materialize()
    return signal


def AddOutput(name, type=1, posedge=None, negedge=None, nodefault=False, async=False):
    raiseOutsideContext('AddOutput')

    names = name.split()
    if len(names) > 1:
        return [AddOutput(n, type, posedge, negedge, nodefault, async) for n in names]
    assert len(names) == 1
    name = names[0]

    if not isinstance(type, int):
        return AddPort(name, type, "output", posedge=posedge, negedge=negedge, nodefault=nodefault, async=async)

    module = tls.ChipyCurrentContext.module

    signal = ChipySignal(module, name)
    signal.width = abs(type)
    signal.signed = type < 0
    signal.outport = True
    signal.register = True
    signal.vlog_lvalue = "__next__" + name
    signal.set_materialize()

    if posedge is not None or negedge is not None:
        AddFF(signal, posedge=posedge, negedge=negedge, nodefault=nodefault)

    if async:
        AddAsync(signal)

    return signal


def AddPort(name, type, role, posedge=None, negedge=None, nodefault=False, async=None):
    bundle = ChipyBundle()

    def addport(port_name, port_type, port_role=None, output=False):
        if role in ("input", "output", "register"):
            port_role = role

        if port_role in ("input", "output"):
            output = (port_role == "output")

        if port_role is None:
            port_role = "output" if output else "input"

        prefix = (name + "__") if name != "" else ""

        if isinstance(port_type, int):
            if role == "register":
                bundle.add(port_name, AddReg(prefix + port_name, port_type, posedge=posedge, negedge=negedge, nodefault=nodefault, async=async))
            elif output:
                bundle.add(port_name, AddOutput(prefix + port_name, port_type, posedge=posedge, negedge=negedge, nodefault=nodefault, async=async))
            else:
                bundle.add(port_name, AddInput(prefix + port_name, port_type))
        else:
            bundle.add(port_name, AddPort(prefix + port_name, port_type, port_role, posedge=posedge, negedge=negedge, nodefault=nodefault, async=async))

    type(addport, role)
    return bundle


def AddReg(name, type=1, posedge=None, negedge=None, nodefault=False, async=None):
    raiseOutsideContext('AddReg')

    names = name.split()
    if len(names) > 1:
        return [AddReg(n, type, posedge, negedge, nodefault, async) for n in names]
    assert len(names) == 1
    name = names[0]

    if not isinstance(type, int):
        return AddPort(name, type, "register", posedge=posedge, negedge=negedge, nodefault=nodefault, async=async)

    module = tls.ChipyCurrentContext.module

    signal = ChipySignal(module, name)
    signal.width = abs(type)
    signal.signed = type < 0
    signal.register = True
    signal.vlog_lvalue = "__next__" + name
    signal.set_materialize()

    if posedge is not None or negedge is not None:
        AddFF(signal, posedge=posedge, negedge=negedge, nodefault=nodefault)

    if async:
        AddAsync(signal)

    return signal


def AddMemory(name, type, depth, posedge=None, negedge=None):
    raiseOutsideContext('AddMemory')

    names = name.split()
    if len(names) > 1:
        return [AddMemory(n, type, depth, posedge, negedge) for n in names]
    assert len(names) == 1
    name = names[0]

    module = tls.ChipyCurrentContext.module

    if isinstance(type, int):
        return ChipyMemory(module, abs(type), depth, name, posedge=posedge, negedge=negedge, signed=(type < 0))

    bundle = Bundle()
    prefix = (name + "__") if name != "" else ""

    def addport(port_name, port_type, port_role=None, output=False):
        bundle.add(port_name, AddMemory(prefix + port_name, port_type, depth, posedge=posedge, negedge=negedge))

    type(addport, "memory")
    return bundle


def AddFF(signal, posedge=None, negedge=None, nodefault=False):
    if isinstance(signal, ChipyBundle):
        for member in signal.members.values():
            AddFF(member, posedge=posedge, negedge=negedge, nodefault=nodefault)
        return

    if not signal.register:
        raise ChipyError('AddFF called on non-register signal')
    if signal.regaction:
        raise ChipyError('AddFF called on register with regaction already set')

    snippet = ChipySnippet()
    if nodefault:
        snippet.text_lines.append(snippet.indent_str + "%s = %d'bx; // %s" % (signal.vlog_lvalue, signal.width, ChipyCodeLoc()))
    else:
        snippet.text_lines.append(snippet.indent_str + "%s = %s; // %s" % (signal.vlog_lvalue, signal.name, ChipyCodeLoc()))
    snippet.lvalue_signals[signal.name] = signal
    signal.module.init_snippets.append(snippet)

    if (posedge is None) == (negedge is None):
        raise ValueError('posedge XOR negedge must be given')

    if posedge is not None:
        signal.module.regactions.append("  always @(posedge %s) %s <= %s; // %s" % (posedge.name, signal.name, signal.vlog_lvalue, ChipyCodeLoc()))
        signal.vlog_reg = True

    if negedge is not None:
        signal.module.regactions.append("  always @(negedge %s) %s <= %s; // %s" % (posedge.name, signal.name, signal.vlog_lvalue, ChipyCodeLoc()))
        signal.vlog_reg = True

    signal.regaction = True


def AddAsync(signal):
    if isinstance(signal, ChipyBundle):
        for member in signal.members.values():
            AddAsync(member)
        return

    if not signal.register:
        raise ChipyError('AddAsync called on non-register signal')
    if signal.regaction:
        raise ChipyError('AddAsync called on register with regaction already set')

    snippet = ChipySnippet()
    snippet.text_lines.append(snippet.indent_str + "%s = %d'bx; // %s" % (signal.vlog_lvalue, signal.width, ChipyCodeLoc()))
    snippet.lvalue_signals[signal.name] = signal
    signal.module.init_snippets.append(snippet)

    signal.module.regactions.append("  assign %s = %s; // %s" % (signal.name, signal.vlog_lvalue, ChipyCodeLoc()))
    signal.regaction = True


def AddInst(name, type):
    raiseOutsideContext('AddInst')

    names = name.split()
    if len(names) > 1:
        return [AddInst(n, type) for n in names]
    assert len(names) == 1
    name = names[0]

    module = tls.ChipyCurrentContext.module

    bundle = AddPort(name, type.intf(), "parent")
    for signal in bundle.values():
        signal.inport = False
        signal.outport = False

    module.instances.append((name, type.name, bundle, ChipyCodeLoc()))
    return bundle


def Cond(cond, if_val, else_val):
    module = ChipySameModule([cond.module, if_val.module, else_val.module])

    signal = ChipySignal(module)
    signal.signed = if_val.signed and else_val.signed
    signal.width = max(if_val.width, else_val.width)
    signal.vlog_rvalue = "%s ? %s : %s" % (cond.name, if_val.name, else_val.name)
    signal.deps[cond.name] = cond
    signal.deps[if_val.name] = if_val
    signal.deps[else_val.name] = else_val
    return signal


def Concat(sigs):
    module = None
    width = 0
    rvalues = list()
    lvalues = list()
    deps = dict()

    if tls.ChipyCurrentContext is not None:
        module = tls.ChipyCurrentContext.module

    for sig in sigs:
        sig = Sig(sig)

        if module is None:
            module = sig.module
        elif sig.module is not None:
            if module is not sig.module:
                raise ChipyError('Trying to Concat signals across module boundaries: {} is in module {} which is not module {}'.format(
                    sig.name, sig.module.name, module.name))

        if sig.vlog_lvalue is None:
            lvalues = None

        if not lvalues is None:
            lvalues.append(sig.vlog_lvalue)

        width += sig.width
        rvalues.append(sig.name)
        deps[sig.name] = sig

    if module is None:
        raise ChipyError('Cannot infer module in Concat. Make sure this is either called from within a module context '
                'or one of the concatenated signals is from within a module.')

    signal = ChipySignal(module)
    signal.width = width
    signal.vlog_rvalue = "{%s}" % ",".join(rvalues)
    if lvalues is not None:
        signal.vlog_lvalue = "{%s}" % ",".join(lvalues)
    signal.deps.update(deps)

    return signal


def Repeat(num, sig):
    sig = Sig(sig)

    if tls.ChipyCurrentContext is not None:
        module = tls.ChipyCurrentContext.module

    signal = ChipySignal(module)
    signal.width = num * sig.width
    signal.vlog_rvalue = "{%d{%s}}" % (num, sig.name)
    signal.deps[sig.name] = sig

    return signal


def Connect(first, second, *rest):
    raiseOutsideContext('Connect')

    sigs = [first, second, *rest]

    if isinstance(first, ChipyBundle):
        for sig in sigs:
            if not isinstance(sig, ChipyBundle):
                raise ValueError('Can only connect bundles with other bundles')
        for member in first.keys():
            Connect(*[sig.get(member) for sig in sigs])
        return

    checkreg = lambda sig: not sig.register or sig.regaction or sig.gotassign
    masters = list(filter(checkreg, sigs))
    # Note that you can't just use something like "sig in sigs" since ChipySignal overloads __eq__.
    slave_sigs = list(filter(lambda s: not checkreg(s), sigs))

    if len(masters) == 0:
        raise ChipyError('Could not identify a master signal in Connect statement')
    if len(masters) > 1:
        raise ChipyError('Multiple possible masters in Connect statement: {}'.format(
            ','.join(sig.name for sig in masters)))

    master_sig, = masters

    module = tls.ChipyCurrentContext.module

    for sig in slave_sigs:
        module.regactions.append("  assign %s = %s; // %s" % (sig.name, master_sig.name, ChipyCodeLoc()))
        sig.portalias = master_sig.name
        sig.register = False
        sig.regaction = False
        sig.gotassign = False
        sig.vlog_reg = False


def Assign(lhs, rhs):
    if isinstance(lhs, ChipyBundle):
        if not isinstance(rhs, ChipyBundle):
            raise ValueError('Can only assign bundles with other bundles')
        for member in lhs.members.keys():
            Assign(lhs.get(member), rhs.get(member))
        return

    lhs = Sig(lhs)
    rhs = Sig(rhs)

    rhs.set_materialize()

    if lhs.memory is not None:
        module = lhs.module
        wen = ChipySignal(module)
        wen.vlog_reg = True
        wen.gotassign = True
        wen.set_materialize()

        snippet = ChipySnippet()
        snippet.text_lines.append(snippet.indent_str + "%s = 1'b0; // %s" % (wen.name, ChipyCodeLoc()))
        snippet.lvalue_signals[wen.name] = wen
        module.init_snippets.append(snippet)

        with ChipyContext() as ctx:
            ctx.add_line("%s = 1'b1; // %s" % (wen.name, ChipyCodeLoc()), wen.get_deps())

        lhs.memory.regactions.append("if (%s) %s <= %s; // %s" % (wen.name, lhs.vlog_rvalue, rhs.name, ChipyCodeLoc()))

        return

    with ChipyContext() as ctx:
        if lhs.vlog_lvalue is None:
            raise ValueError('Trying to assign to signal with unset lvalue')
        lhs_deps = lhs.get_deps()
        for lhs_dep in lhs_deps.values():
            lhs_dep.gotassign = True

        ctx.add_line("%s = %s; // %s" % (lhs.vlog_lvalue, rhs.name, ChipyCodeLoc()), lhs_deps)


def Sig(arg, width=None):
    if isinstance(arg, ChipySignal,):
        if width is not None:
            module = ChipySameModule([arg.module])
            signal = ChipySignal(module)
            signal.signed = width < 0
            signal.width = abs(width)
            signal.vlog_rvalue = arg.name
            signal.deps[arg.name] = arg
            return signal
        return arg

    if isinstance(arg, (tuple, list)):
        if width is not None:
            raise ValueError('When constructing Sig from tuple or list, width must not be given')
        return Concat(arg)

    if isinstance(arg, str):
        if width is not None:
            raise ValueError('When constructing Sig from name, width must not be given')
        if arg not in tls.ChipyCurrentContext.module.signals:
            raise ChipyError('Signal {} not found in current module ({})'.format(
                arg, tls.ChipyCurrentContext.module.name))
        return tls.ChipyCurrentContext.module.signals[arg]

    if isinstance(arg, int):
        if width is None: width=-32
        signal = ChipySignal(None, "%s'%sd%d" % (abs(width), "s" if width < 0 else "", arg), True)
        signal.signed = width < 0
        signal.width = abs(width)
        return signal

    raise TypeError('Cannot construct Sig from object of type {}'.format(type(arg)))


@contextmanager
def If(cond):
    tls.ChipyElseContext = None
    cond.set_materialize()
    with ChipyContext().block("if (%s) begin // %s" % (cond.name, ChipyCodeLoc())) as ctx:
        yield
        tls.ChipyElseContext = ctx


@contextmanager
def ElseIf(cond):
    cond = Sig(cond)

    tls.ChipyElseContext = None
    cond.set_materialize()
    with tls.ChipyElseContext.block("else if (%s) begin // %s" % (cond.name, ChipyCodeLoc())) as ctx:
        yield
        tls.ChipyElseContext = ctx


@contextmanager
def Else():
    if tls.ChipyElseContext is None:
        raise ChipyError('Cannot find matching If/IfElse for Else')
    with tls.ChipyElseContext as ctx:
        ctx.add_line("else begin // %s" % ChipyCodeLoc())
        ctx.add_indent()

        yield

        tls.ChipyElseContext = ctx
        ctx.remove_indent()
        ctx.add_line("end")


@contextmanager
def Switch(expr, parallel=False, full=False):
    expr = Sig(expr)

    tls.ChipyElseContext = None
    ctx = ChipyContext()
    expr.set_materialize()
    if parallel:
        ctx.add_line("(* parallel_case *)")
    if full:
        ctx.add_line("(* full_case *)")
    with ctx.block(
            begin="case (%s) // %s" % (expr.name, ChipyCodeLoc()),
            end='endcase'):
        yield
        tls.ChipyElseContext = None


@contextmanager
def Case(expr):
    expr = Sig(expr)
    expr.set_materialize()
    tls.ChipyElseContext = None
    with ChipyContext().block("%s: begin // %s" % (expr.name, ChipyCodeLoc())) as ctx:
        yield
        tls.ChipyElseContext = None


@contextmanager
def Default():
    tls.ChipyElseContext = None
    with ChipyContext().block("default: begin // %s" % ChipyCodeLoc()) as ctx:
        yield
        tls.ChipyElseContext = None


def Stream(data_type, last=False, destbits=0):
    def callback(addport, role):
        addport("valid", 1, output=(role == "master"))
        addport("ready", 1, output=(role == "slave"))
        addport("data", data_type, output=(role == "master"))
        if last:
            addport("last", 1, output=(role == "master"))
        if destbits != 0:
            addport("dest", destbits, output=(role == "master"))
    return callback


def WriteVerilog(f):
    print("// Generated using Chipy (Constructing Hardware In PYthon)", file=f)
    for modname, module in tls.ChipyModulesDict.items():
        module.write_verilog(f)
