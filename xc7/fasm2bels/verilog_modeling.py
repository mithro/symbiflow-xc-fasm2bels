""" Core classes for modelling a bitstream back into verilog and routes.

There are 3 modelling elements:

 - Bel: A synthesizable element.
 - Site: A collection of Bel's, routing sinks and routing sources.
 - Module: The root container for all Sites

The modelling approach works as so:

BELs represent a particular tech library instance (e.g. LUT6 or FDRE).  These
BELs are connected into the routing fabric or internal site sources via the
Site methods:

 - Site.add_sink
 - Site.add_source
 - Site.add_output_from_internal
 - Site.connect_internal
 - Site.add_internal_source

BEL parameters should be on the BEL.

In cases where there is multiple instances of a BEL (e.g. LUT's), the
Bel.set_bel must be called to ensure that Vivado places the BEL in the exact
location.

"""

import functools
from .make_routes import make_routes, ONE_NET, ZERO_NET, prune_antennas
from .connection_db_utils import get_wire_pkey


class Bel(object):
    """ Object to model a BEL.
    """

    def __init__(self, module, name=None, keep=True):
        """ Construct Bel object.

        module (str): Exact tech library name to instance during synthesis.
            Example "LUT6_2" or "FDRE".
        name (str): Optional name of this bel, used to disambiguate multiple
            instances of the same module in a site.  If there are multiple
            instances of the same module in a site, name must be specified and
            unique.
        keep (bool): Controls if KEEP, DONT_TOUCH constraints are added to this
            instance.

        """
        self.module = module
        if name is None:
            self.name = module
        else:
            self.name = name
        self.connections = {}
        self.unused_connections = set()
        self.parameters = {}
        self.outputs = set()
        self.prefix = None
        self.site = None
        self.keep = keep
        self.bel = None
        self.nets = None

    def set_prefix(self, prefix):
        """ Set the prefix used for wire and BEL naming.

        This is method is typically called automatically during
        Site.integrate_site. """
        self.prefix = prefix

    def set_site(self, site):
        """ Sets the site string used to set the LOC constraint.

        This is method is typically called automatically during
        Site.integrate_site. """
        self.site = site

    def set_bel(self, bel):
        """ Sets the BEL constraint.

        This method should be called if the parent site has multiple instances
        of the BEL (e.g. LUT6 in a SLICE).
        """
        self.bel = bel

    def _prefix_things(self, s):
        """ Apply the prefix (if any) to the input string. """
        if self.prefix is not None:
            return '{}_{}'.format(self.prefix, s)
        else:
            return s

    def get_cell(self):
        """ Get the cell name of this BEL.

        Should only be called after set_prefix has been invoked (if set_prefix
        will be called)."""
        return self._prefix_things(self.name)

    def output_verilog(self, top, indent='  '):
        """ Output the Verilog to represent this BEL. """
        connections = {}
        buses = {}
        bus_is_output = {}

        for wire, connection in self.connections.items():
            if top.is_top_level(connection):
                connection_wire = connection
            elif connection in [0, 1]:
                connection_wire = connection
            else:
                if connection is not None:
                    connection_wire = self._prefix_things(connection)
                else:
                    connection_wire = None

            if '[' in wire:
                bus_name, address = wire.split('[')
                assert address[-1] == ']', address

                wire_is_output = wire in self.outputs
                if bus_name not in buses:
                    buses[bus_name] = {}
                    bus_is_output[bus_name] = wire_is_output
                else:
                    assert bus_is_output[bus_name] == wire_is_output, (
                        bus_name, wire,
                        bus_is_output[bus_name],
                        wire_is_output,
                        )

                buses[bus_name][int(address[:-1])] = connection_wire
            else:
                connections[wire
                            ] = '{indent}{indent}.{wire}({connection})'.format(
                                indent=indent,
                                wire=wire,
                                connection=connection_wire
                            )

        yield ''

        for bus_name, bus in buses.items():
            bus_wire = self._prefix_things(bus_name)
            connections[bus_name
                        ] = '{indent}{indent}.{bus_name}({bus_wire})'.format(
                            indent=indent,
                            bus_name=bus_name,
                            bus_wire=bus_wire,
                        )

            yield '{indent}wire [{width_n1}:0] {bus_wire};'.format(
                indent=indent,
                bus_wire=bus_wire,
                width_n1=max(bus.keys()),
            )

            for idx, wire in bus.items():
                if wire is None:
                    continue

                if bus_is_output[bus_name]:
                    yield '{indent}assign {wire} = {bus_wire}[{idx}];'.format(
                        indent=indent,
                        bus_wire=bus_wire,
                        idx=idx,
                        wire=wire,
                    )
                else:
                    yield '{indent}assign {bus_wire}[{idx}] = {wire};'.format(
                        indent=indent,
                        bus_wire=bus_wire,
                        idx=idx,
                        wire=wire,
                    )

        for unused_connection in self.unused_connections:
            connections[unused_connection
                        ] = '{indent}{indent}.{connection}()'.format(
                            indent=indent, connection=unused_connection
                        )

        yield ''

        if self.site is not None:
            comment = []
            if self.keep:
                comment.append('KEEP')
                comment.append('DONT_TOUCH')

            comment.append('LOC = "{site}"'.format(site=self.site))

            if self.bel:
                comment.append('BEL = "{bel}"'.format(bel=self.bel))

            yield '{indent}(* {comment} *)'.format(
                indent=indent, comment=', '.join(comment)
            )

        yield '{indent}{site} #('.format(indent=indent, site=self.module)

        parameters = []
        for param, value in sorted(self.parameters.items(),
                                   key=lambda x: x[0]):
            parameters.append(
                '{indent}{indent}.{param}({value})'.format(
                    indent=indent, param=param, value=value
                )
            )

        if parameters:
            yield ',\n'.join(parameters)

        yield '{indent}) {name} ('.format(indent=indent, name=self.get_cell())

        if connections:
            yield ',\n'.join(connections[port] for port in sorted(connections))

        yield '{indent});'.format(indent=indent)


class Site(object):
    """ Object to model a Site.

    A site is a collection of BELs, and sources and sinks that connect the
    site to the routing fabric.  Sources and sinks exported by the Site will
    be used during routing formation.

    Wires that are not in the sources and sinks lists will be invisible to
    the routing formation step.  In particular, site connections that should
    be sources and sinks but are not specified will be ingored during routing
    formation, and likely end up as disconnected wires.

    On the flip side it is import that specified sinks are always connected
    to at least one BEL.  If this is not done, antenna nets may be emitted
    during routing formation, which will result in a DRC violation.

    """

    def __init__(self, features, site, tile=None):
        self.bels = []
        self.sinks = {}
        self.sources = {}
        self.outputs = {}
        self.internal_sources = {}

        self.set_features = set()
        self.post_route_cleanup = None
        self.bel_map = {}

        self.site_wire_to_wire_pkey = {}

        aparts = features[0].feature.split('.')

        for f in features:
            if f.value == 0:
                continue

            parts = f.feature.split('.')
            assert parts[0] == aparts[0]
            assert parts[1] == aparts[1]
            self.set_features.add('.'.join(parts[2:]))

        if tile is None:
            self.tile = aparts[0]
        else:
            self.tile = tile

        self.site = site

    def has_feature(self, feature):
        """ Does this set have the specified feature set? """
        return feature in self.set_features

    def add_sink(self, bel, bel_pin, sink):
        """ Adds a sink.

        Attaches sink to the specified bel.

        bel (Bel): Bel object
        bel_pin (str): The exact tech library name for the relevant pin.  Can be
            a bus (e.g. A[5]).  The name must identically match the library
            name or an error will occur during synthesis.
        sink (str): The exact site pin name for this sink.  The name must
            identically match the site pin name, or an error will be generated
            when Site.integrate_site is invoked.

        """

        assert bel_pin not in bel.connections

        if sink not in self.sinks:
            self.sinks[sink] = []

        bel.connections[bel_pin] = sink
        self.sinks[sink].append((bel, bel_pin))

    def add_source(self, bel, bel_pin, source):
        """ Adds a source.

        Attaches source to bel.

        bel (Bel): Bel object
        bel_pin (str): The exact tech library name for the relevant pin.  Can be
            a bus (e.g. A[5]).  The name must identically match the library
            name or an error will occur during synthesis.
        source (str): The exact site pin name for this source.  The name must
            identically match the site pin name, or an error will be generated
            when Site.integrate_site is invoked.

        """
        assert source not in self.sources
        assert bel_pin not in bel.connections

        bel.connections[bel_pin] = source
        bel.outputs.add(bel_pin)
        self.sources[source] = (bel, bel_pin)

    def add_output_from_internal(self, source, internal_source):
        """ Adds a source from a site internal source.

        This is used to convert an internal_source wire to a site source.

        source (str): The exact site pin name for this source.  The name must
            identically match the site pin name, or an error will be generated
            when Site.integrate_site is invoked.
        internal_source (str): The internal_source must match the internal
            source name provided to Site.add_internal_source earlier.

        """
        assert source not in self.sources
        assert internal_source in self.internal_sources

        self.outputs[source] = internal_source
        self.sources[source] = self.internal_sources[internal_source]

    def add_output_from_output(self, source, other_source):
        """ Adds an output wire from an existing source wire.

        The new output wire is not a source, but will participate in routing
        formation.

        source (str): The exact site pin name for this source.  The name must
            identically match the site pin name, or an error will be generated
            when Site.integrate_site is invoked.
        other_source (str): The name of an existing source generated from add_source.

        """
        assert source not in self.sources
        assert other_source in self.sources
        self.outputs[source] = other_source

    def add_internal_source(self, bel, bel_pin, wire_name):
        """ Adds a site internal source.

        Adds an internal source to the site.  This wire will not be used during
        routing formation, but can be connected to other BELs within the site.

        bel (Bel): Bel object
        bel_pin (str): The exact tech library name for the relevant pin.  Can be
            a bus (e.g. A[5]).  The name must identically match the library
            name or an error will occur during synthesis.
        wire_name (str): The name of the site wire.  This wire_name must be
            overlap with a source or sink site pin name.

        """
        bel.connections[bel_pin] = wire_name
        bel.outputs.add(bel_pin)

        assert wire_name not in self.internal_sources, wire_name
        self.internal_sources[wire_name] = (bel, bel_pin)

    def connect_internal(self, bel, bel_pin, source):
        """ Connect a BEL pin to an existing internal source.

        bel (Bel): Bel object
        bel_pin (str): The exact tech library name for the relevant pin.  Can be
            a bus (e.g. A[5]).  The name must identically match the library
            name or an error will occur during synthesis.
        source (str): Existing internal source wire added via
            add_internal_source.

        """
        assert source in self.internal_sources, source
        assert bel_pin not in bel.connections
        bel.connections[bel_pin] = source

    def add_bel(self, bel, name=None):
        """ Adds a BEL to the site.

        All BELs that use the add_sink, add_source, add_internal_source,
        and connect_internal must call add_bel with the relevant BEL.

        bel (Bel): Bel object
        name (str): Optional name to assign to the bel to enable retrival with
            the maybe_get_bel method.  This name is not used for any other
            reason.

        """

        self.bels.append(bel)
        if name is not None:
            assert name not in self.bel_map
            self.bel_map[name] = bel

    def set_post_route_cleanup_function(self, func):
        """ Set callback to be called on this site during routing formation.

        This callback is intended to enable sites that must perform decisions
        based on routed connections.

        func (function): Function that takes two arguments, the parent module
            and the site object to cleanup.

        """
        self.post_route_cleanup = func

    def integrate_site(self, conn, module):
        """ Integrates site so that it can be used with routing formation.

        This method is called automatically by Module.add_site.

        """
        self.check_site()

        prefix = '{}_{}'.format(self.tile, self.site.name)

        site_pin_map = make_site_pin_map(frozenset(self.site.site_pins))

        # Sanity check BEL connections
        for bel in self.bels:
            bel.set_prefix(prefix)
            bel.set_site(self.site.name)

            for wire in bel.connections.values():
                if wire == 0 or wire == 1:
                    continue

                assert wire in self.sinks or \
                       wire in self.sources or \
                       wire in self.internal_sources or \
                       module.is_top_level(wire), wire

        wires = set()
        unrouted_sinks = set()
        unrouted_sources = set()
        wire_pkey_to_wire = {}
        source_bels = {}
        wire_assigns = {}

        for wire in self.internal_sources:
            prefix_wire = prefix + '_' + wire
            wires.add(prefix_wire)

        for wire in self.sinks:
            if wire is module.is_top_level(wire):
                continue

            prefix_wire = prefix + '_' + wire
            wires.add(prefix_wire)
            wire_pkey = get_wire_pkey(conn, self.tile, site_pin_map[wire])
            wire_pkey_to_wire[wire_pkey] = prefix_wire
            self.site_wire_to_wire_pkey[wire] = wire_pkey
            unrouted_sinks.add(wire_pkey)

        for wire in self.sources:
            if wire is module.is_top_level(wire):
                continue

            prefix_wire = prefix + '_' + wire
            wires.add(prefix_wire)
            wire_pkey = get_wire_pkey(conn, self.tile, site_pin_map[wire])
            wire_pkey_to_wire[wire_pkey] = prefix_wire
            self.site_wire_to_wire_pkey[wire] = wire_pkey
            unrouted_sources.add(wire_pkey)

            source_bel = self.sources[wire]

            if source_bel is not None:
                source_bels[wire_pkey] = source_bel

        shorted_nets = {}

        for source_wire, sink_wire in self.outputs.items():
            wire_source = prefix + '_' + sink_wire
            wire = prefix + '_' + source_wire
            wires.add(wire)
            wire_assigns[wire] = wire_source

            # If this is a passthrough wire, then indicate that allow the net
            # is be merged.
            if sink_wire not in site_pin_map:
                continue

            sink_wire_pkey = get_wire_pkey(
                conn, self.tile, site_pin_map[sink_wire]
            )
            source_wire_pkey = get_wire_pkey(
                conn, self.tile, site_pin_map[source_wire]
            )
            if sink_wire_pkey in unrouted_sinks:
                shorted_nets[source_wire_pkey] = sink_wire_pkey

                # Because this is being treated as a short, remove the
                # source and sink.
                unrouted_sources.remove(source_wire_pkey)
                unrouted_sinks.remove(sink_wire_pkey)

        return dict(
            wires=wires,
            unrouted_sinks=unrouted_sinks,
            unrouted_sources=unrouted_sources,
            wire_pkey_to_wire=wire_pkey_to_wire,
            source_bels=source_bels,
            wire_assigns=wire_assigns,
            shorted_nets=shorted_nets,
        )

    def check_site(self):
        """ Sanity checks that the site is internally consistent. """
        internal_sources = set(self.internal_sources.keys())
        sinks = set(self.sinks.keys())
        sources = set(self.sources.keys())

        assert len(internal_sources & sinks) == 0, (internal_sources & sinks)
        assert len(internal_sources & sources) == 0, (
            internal_sources & sources
        )

        bel_ids = set()
        for bel in self.bels:
            bel_ids.add(id(bel))

        for bel_pair in self.sources.values():
            if bel_pair is not None:
                bel, _ = bel_pair
                assert id(bel) in bel_ids

        for sinks in self.sinks.values():
            for bel, _ in sinks:
                assert id(bel) in bel_ids

        for bel_pair in self.internal_sources.values():
            if bel_pair is not None:
                bel, _ = bel_pair
                assert id(bel) in bel_ids

    def maybe_get_bel(self, name):
        """ Returns named BEL from site.

        name (str): Name given during Site.add_bel.

        Returns None if name is not found, otherwise Bel object.
        """
        if name in self.bel_map:
            return self.bel_map[name]
        else:
            return None

    def remove_bel(self, bel_to_remove):
        """ Attempts to remove BEL from site.

        It is an error to remove a BEL if any of its outputs are currently
        in use by the Site.  This method does NOT verify that the sources
        of the BEL are not currently in use.

        """
        bel_idx = None
        for idx, bel in enumerate(self.bels):
            if id(bel) == id(bel_to_remove):
                bel_idx = idx
                break

        assert bel_idx is not None

        # Make sure none of the BEL sources are in use
        for bel in self.bels:
            if id(bel) == id(bel_to_remove):
                continue

            for site_wire in bel.connections.values():
                assert site_wire not in bel_to_remove.outputs, site_wire

        # BEL is not used internal, preceed with removal.
        del self.bels[bel_idx]
        removed_sinks = []
        removed_sources = []

        for sink_wire, bels_using_sink in self.sinks.items():
            bel_idx = None
            for idx, (bel, _) in enumerate(bels_using_sink):
                if id(bel) == id(bel_to_remove):
                    bel_idx = idx
                    break

            if bel_idx is not None:
                del bels_using_sink[bel_idx]

            if len(bels_using_sink) == 0:
                removed_sinks.append(self.site_wire_to_wire_pkey[sink_wire])

        sources_to_remove = []
        for source_wire, (bel, _) in self.sources.items():
            if id(bel) == id(bel_to_remove):
                removed_sources.append(
                    self.site_wire_to_wire_pkey[source_wire]
                )
                sources_to_remove.append(source_wire)

        for wire in sources_to_remove:
            del self.sources[wire]

        return removed_sinks, removed_sources

    def find_internal_source(self, bel, internal_source):
        source_wire = bel.connections[internal_source]
        assert source_wire in self.internal_sources, (
            internal_source, source_wire
        )

        for source, (bel_source, bel_wire) in self.sources.items():
            if id(bel_source) != id(bel):
                continue

            if bel_wire == internal_source:
                continue

            return source

        return None

    def find_internal_sink(self, bel, internal_sink):
        sink_wire = bel.connections[internal_sink]
        assert sink_wire not in bel.outputs, (internal_sink, sink_wire)

        if sink_wire not in self.internal_sources:
            assert sink_wire in self.sinks
            return sink_wire

    def remove_internal_sink(self, bel, internal_sink):
        sink_wire = self.find_internal_sink(bel, internal_sink)
        bel.connections[internal_sink] = None
        if sink_wire is not None:
            idx_to_remove = []
            for idx, (other_bel,
                      other_internal_sink) in enumerate(self.sinks[sink_wire]):
                if id(bel) == id(other_bel):
                    assert other_internal_sink == internal_sink
                    idx_to_remove.append(idx)

            for idx in sorted(idx_to_remove)[::-1]:
                del self.sinks[sink_wire][idx]

            if len(self.sinks[sink_wire]) == 0:
                del self.sinks[sink_wire]
                return self.site_wire_to_wire_pkey[sink_wire]


@functools.lru_cache(maxsize=None)
def make_site_pin_map(site_pins):
    """ Create map of site pin names to tile wire names. """
    site_pin_map = {}

    for site_pin in site_pins:
        site_pin_map[site_pin.name] = site_pin.wire

    return site_pin_map


def merge_exclusive_sets(set_a, set_b):
    """ set_b into set_a after verifying that set_a and set_b are disjoint. """
    assert len(set_a & set_b) == 0, (set_a & set_b)

    set_a |= set_b


def merge_exclusive_dicts(dict_a, dict_b):
    """ dict_b into dict_a after verifying that dict_a and dict_b have disjoint keys. """
    assert len(set(dict_a.keys()) & set(dict_b.keys())) == 0

    dict_a.update(dict_b)


def make_bus(wires):
    """ Combine bus wires into a consecutive bus.

    >>> list(make_bus(['A', 'B']))
    [('A', None), ('B', None)]
    >>> list(make_bus(['A[0]', 'A[1]', 'B']))
    [('A', 2), ('B', None)]
    >>> list(make_bus(['A[0]', 'A[1]', 'B[0]']))
    [('A', 2), ('B', 1)]

    """
    output = {}
    buses = {}

    for w in wires:
        idx = w.find('[')
        if idx != -1:
            assert w[-1] == ']', w

            bus = w[0:idx]
            if bus not in buses:
                buses[bus] = []

            buses[bus].append(int(w[idx + 1:-1]))
        else:
            output[w] = None

    for bus, values in buses.items():
        assert min(values) == 0
        assert max(values) == len(values) - 1
        assert len(values) == len(set(values)), (bus, values)
        output[bus] = max(values)

    for name in sorted(output):
        yield name, output[name]


class Module(object):
    """ Object to model a design. """

    def __init__(self, db, grid, conn, name="top"):
        self.name = name
        self.iostandard = None
        self.db = db
        self.grid = grid
        self.conn = conn
        self.sites = []
        self.source_bels = {}

        # Map of source to sink.
        self.shorted_nets = {}

        # Map of wire_pkey to Verilog wire.
        self.wire_pkey_to_wire = {}

        # wire_pkey of sinks that are not connected to their routing.
        self.unrouted_sinks = set()

        # wire_pkey of sources that are not connected to their routing.
        self.unrouted_sources = set()

        # Known active pips, tuples of sink and source wire_pkey's.
        # The sink wire_pkey is a net with the source wire_pkey.
        self.active_pips = set()

        self.root_in = set()
        self.root_out = set()
        self.root_inout = set()

        self.wires = set()
        self.wire_assigns = {}

        # Optional map of site to signal names.
        # This was originally intended for IPAD and OPAD signal naming.
        self.site_to_signal = {}

    def set_iostandard(self, iostandards):
        """ Set the IOSTANDARD for the design.

        iostandards (list of list of str): Takes a list of IOSTANDARD
            possibilities and selects the unique one.  Having no valid or
            multiple valid IOSTANDARDs is an error.
        """
        possible_iostandards = set(iostandards[0])

        for l in iostandards:
            possible_iostandards &= set(l)

        if len(possible_iostandards) != 1:
            raise RuntimeError(
                'Ambigous IOSTANDARD, must specify possibilities: {}'.
                format(possible_iostandards)
            )

        self.iostandard = possible_iostandards.pop()

    def set_site_to_signal(self, site_to_signal):
        """ Assing site to signal map for top level sites.

        Args:
            site_to_signal (dict): Site to signal name map

        """
        self.site_to_signal = site_to_signal

    def _check_top_name(self, tile, site, name):
        """ Returns top level port name for given tile and site

        Args:
            tile (str): Tile containing site
            site (str): Site containing top level pad.
            name (str): User-defined pad name (e.g. IPAD or OPAD, etc).

        """
        if site not in self.site_to_signal:
            return '{}_{}_{}'.format(tile, site, name)
        else:
            return self.site_to_signal[site]

    def add_top_in_port(self, tile, site, name):
        """ Add a top level input port.

        tile (str): Tile name that will sink the input port.
        site (str): Site name that will sink the input port.
        name (str): Name of port.

        Returns str of root level port name.
        """

        port = self._check_top_name(tile, site, name)
        assert port not in self.root_in
        self.root_in.add(port)

        return port

    def add_top_out_port(self, tile, site, name):
        """ Add a top level output port.

        tile (str): Tile name that will sink the output port.
        site (str): Site name that will sink the output port.
        name (str): Name of port.

        Returns str of root level port name.
        """
        port = self._check_top_name(tile, site, name)
        assert port not in self.root_out
        self.root_out.add(port)

        return port

    def add_top_inout_port(self, tile, site, name):
        """ Add a top level inout port.

        tile (str): Tile name that will sink the inout port.
        site (str): Site name that will sink the inout port.
        name (str): Name of port.

        Returns str of root level port name.
        """
        port = self._check_top_name(tile, site, name)
        assert port not in self.root_inout
        self.root_inout.add(port)

        return port

    def is_top_level(self, wire):
        """ Returns true if specified wire is a top level wire. """
        return wire in self.root_in or wire in self.root_out or wire in self.root_inout

    def add_site(self, site):
        """ Adds a site to the module. """
        integrated_site = site.integrate_site(self.conn, self)

        merge_exclusive_sets(self.wires, integrated_site['wires'])
        merge_exclusive_sets(
            self.unrouted_sinks, integrated_site['unrouted_sinks']
        )
        merge_exclusive_sets(
            self.unrouted_sources, integrated_site['unrouted_sources']
        )

        merge_exclusive_dicts(
            self.wire_pkey_to_wire, integrated_site['wire_pkey_to_wire']
        )
        merge_exclusive_dicts(self.source_bels, integrated_site['source_bels'])
        merge_exclusive_dicts(
            self.wire_assigns, integrated_site['wire_assigns']
        )
        merge_exclusive_dicts(
            self.shorted_nets, integrated_site['shorted_nets']
        )

        self.sites.append(site)

    def make_routes(self, allow_orphan_sinks):
        """ Create nets from top level wires, activie PIPS, sources and sinks.

        Invoke make_routes after all sites and pips have been added.

        allow_orphan_sinks (bool): Controls whether it is an error if a sink
            has no source.

        """
        self.nets = {}
        self.net_map = {}
        for sink_wire, src_wire in make_routes(
                db=self.db,
                conn=self.conn,
                wire_pkey_to_wire=self.wire_pkey_to_wire,
                unrouted_sinks=self.unrouted_sinks,
                unrouted_sources=self.unrouted_sources,
                active_pips=self.active_pips,
                allow_orphan_sinks=allow_orphan_sinks,
                shorted_nets=self.shorted_nets,
                nets=self.nets,
                net_map=self.net_map,
        ):
            self.wire_assigns[sink_wire] = src_wire

        self.handle_post_route_cleanup()

    def output_verilog(self):
        """ Yields lines of verilog that represent the design in Verilog.

        Invoke output_verilog after invoking make_routes to ensure that
        inter-site connections are made.

        """
        root_module_args = []

        for in_wire, width in make_bus(self.root_in):
            if width is None:
                root_module_args.append('  input ' + in_wire)
            else:
                root_module_args.append(
                    '  input [{}:0] {}'.format(width, in_wire)
                )

        for out_wire, width in make_bus(self.root_out):
            if width is None:
                root_module_args.append('  output ' + out_wire)
            else:
                root_module_args.append(
                    '  output [{}:0] {}'.format(width, out_wire)
                )

        for inout_wire, width in make_bus(self.root_inout):
            if width is None:
                root_module_args.append('  inout ' + inout_wire)
            else:
                root_module_args.append(
                    '  inout [{}:0] {}'.format(width, inout_wire)
                )

        yield 'module {}('.format(self.name)

        yield ',\n'.join(root_module_args)

        yield '  );'

        for wire in sorted(self.wires):
            yield '  wire {};'.format(wire)

        for site in self.sites:
            for bel in site.bels:
                yield ''
                for l in bel.output_verilog(self, indent='  '):
                    yield l

        for lhs, rhs in self.wire_assigns.items():
            yield '  assign {} = {};'.format(lhs, rhs)

        yield 'endmodule'

    def output_bel_locations(self):
        """ Yields lines of tcl that will assign set the location of BELs. """
        for bel in self.get_bels():
            yield """
set cell [get_cells {cell}]
if {{ $cell == {{}} }} {{
    error "Failed to find cell!"
}}
set_property LOC [get_sites {site}] $cell""".format(
                cell=bel.get_cell(), site=bel.site
            )

            if bel.bel is not None:
                yield """
set_property BEL "[get_property SITE_TYPE [get_sites {site}]].{bel}" $cell""".format(
                    site=bel.site,
                    bel=bel.bel,
                )

    def output_nets(self):
        """ Yields lines of tcl that will assign the exact routing path for nets.

        Invoke output_nets after invoking make_routes.

        """
        assert len(self.nets) > 0

        for net_wire_pkey, net in self.nets.items():
            if net_wire_pkey == ZERO_NET:
                yield 'set net [get_nets {<const0>}]'
            elif net_wire_pkey == ONE_NET:
                yield 'set net [get_nets {<const1>}]'
            else:
                if net_wire_pkey not in self.source_bels:
                    continue

                if not net.is_net_alive():
                    continue

                bel, pin = self.source_bels[net_wire_pkey]

                yield """
set pin [get_pins {cell}/{pin}]
if {{ $pin == {{}} }} {{
    error "Failed to find pin!"
}}
set net [get_nets -of_object $pin]
if {{ $net == {{}} }} {{
    error "Failed to find net!"
}}
""".format(
                    cell=bel.get_cell(),
                    pin=pin,
                )

            yield """
set_property FIXED_ROUTE {fixed_route} $net
""".format(
                fixed_route=' '.
                join(net.make_fixed_route(self.conn, self.wire_pkey_to_wire))
            )

    def get_bels(self):
        """ Yield a list of Bel objects in the module. """
        for site in self.sites:
            for bel in site.bels:
                yield bel

    def handle_post_route_cleanup(self):
        """ Handle post route clean-up. """
        for site in self.sites:
            if site.post_route_cleanup is not None:
                site.post_route_cleanup(self, site)

        prune_antennas(self.conn, self.nets, self.unrouted_sinks)

    def find_sinks_from_source(self, site, site_wire):
        """ Yields sink wire names from a site wire source. """
        wire_pkey = site.site_wire_to_wire_pkey[site_wire]
        assert wire_pkey in self.nets

        source_wire = self.wire_pkey_to_wire[wire_pkey]

        for sink_wire, other_source_wire in self.wire_assigns.items():
            if source_wire == other_source_wire:
                yield sink_wire

    def remove_bel(self, site, bel):
        """ Remove a BEL from the module.

        If this is the last use of a site sink, then that wire and wire
        connection is removed.
        """

        removed_sinks, removed_sources = site.remove_bel(bel)

        # Make sure none of the sources are in use.
        for wire_pkey in removed_sources:
            source_wire = self.wire_pkey_to_wire[wire_pkey]

            for _, other_source_wire in self.wire_assigns.items():
                assert source_wire != other_source_wire, source_wire

            self.unrouted_sources.remove(wire_pkey)
            del self.source_bels[wire_pkey]

        # Remove the sinks from the wires, wire assigns, and net
        for wire_pkey in removed_sinks:
            self.remove_sink(wire_pkey)

    def remove_sink(self, wire_pkey):
        self.unrouted_sinks.remove(wire_pkey)
        self.wires.remove(self.wire_pkey_to_wire[wire_pkey])
        sink_wire = self.wire_pkey_to_wire[wire_pkey]
        if sink_wire in self.wire_assigns:
            del self.wire_assigns[sink_wire]
