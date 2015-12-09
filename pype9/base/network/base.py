"""
  Author: Thomas G. Close (tclose@oist.jp)
  Copyright: 2012-2014 Thomas G. Close.
  License: This file is part of the "NineLine" package, which is released under
           the MIT Licence, see LICENSE for details.
"""
from __future__ import absolute_import
from collections import namedtuple, defaultdict
from itertools import chain
from nineml.user import ComponentArray, Initial, Property
from pype9.exceptions import Pype9RuntimeError
from .values import get_pyNN_value
import os.path
import nineml
from nineml import units as un
from pyNN.random import NumpyRNG
import pyNN.standardmodels
import quantities as pq
from nineml.user.multi import (
    MultiDynamicsProperties, append_namespace, BasePortExposure)
from nineml.user.network import (
    ComponentArray as ComponentArray9ML,
    EventConnectionGroup as EventConnGroup9ML,
    AnalogConnectionGroup as AnalogConnGroup9ML)
from pype9.exceptions import Pype9UnflattenableException
from nineml.values import SingleValue
from .connectivity import InversePyNNConnectivity
from ..cells import (
    DynamicsWithSynapsesProperties, ConnectionProperty, SynapseProperties)


_REQUIRED_SIM_PARAMS = ['timestep', 'min_delay', 'max_delay', 'temperature']


class Network(object):

    # Name given to the "cell" component of the cell dynamics + linear synapse
    # dynamics multi-dynamics
    cell_dyn_name = 'cell'

    def __init__(self, nineml_model, build_mode='lazy',
                 timestep=None, min_delay=None, max_delay=None,
                 temperature=None, rng=None, **kwargs):
        self._nineml = nineml_model
        if isinstance(nineml_model, basestring):
            nineml_model = nineml.read(nineml_model).as_network()
        self._set_simulation_params(timestep=timestep, min_delay=min_delay,
                                    max_delay=max_delay,
                                    temperature=temperature)
        self._rng = rng if rng else NumpyRNG()
        self._component_arrays = {}
        for name, comp_array in self.nineml_model.component_arrays.iteritems():
            self._component_arrays[name] = self.ComponentArrayClass(
                comp_array, rng=self._rng, build_mode=build_mode, **kwargs)
        if build_mode not in ('build_only', 'compile_only'):
            # Set the connectivity objects of the projections to the
            # PyNNConnectivity class
            if nineml_model.connectivity_has_been_sampled():
                raise Pype9RuntimeError(
                    "Connections have already been sampled, please reset them"
                    " using 'resample_connectivity' before constructing "
                    "network")
            nineml_model.resample_connectivity(
                connectivity_class=self.ConnectivityClass)
            self._connection_groups = {}
            for conn_group in nineml_model.connection_groups:
                self._connection_groups[
                    conn_group.name] = self.ConnectionGroupClass(
                        conn_group, rng=self._rng)
            self._finalise_construction()

    def _finalise_construction(self):
        """
        Can be overriden by deriving classes to do any simulator-specific
        finalisation that is required
        """
        pass

    @property
    def component_arrays(self):
        return self._component_arrays

    @property
    def connection_groups(self):
        return self._connection_groups

    def save_connections(self, output_dir):
        """
        Saves generated connections to output directory

        @param output_dir:
        """
        for conn_grp in self.connection_groups.itervalues():
            if isinstance(conn_grp.synapse_type,
                          pyNN.standardmodels.synapses.ElectricalSynapse):
                attributes = 'weight'
            else:
                attributes = 'all'
            conn_grp.save(attributes, os.path.join(
                output_dir, conn_grp.label + '.proj'), format='list',
                gather=True)

    def record(self, variable):
        """
        Record variable from complete network
        """
        for comp_array in self.component_arrays.itervalues():
            comp_array.record(variable)

    def write_data(self, file_prefix, **kwargs):
        """
        Record all spikes generated in the network

        @param filename: The prefix for every population files before the
                         popluation name. The suffix '.spikes' will be
                         appended to the filenames as well.
        """
        # Add a dot to separate the prefix from the population label if it
        # doesn't already have one and isn't a directory
        if (not os.path.isdir(file_prefix) and not file_prefix.endswith('.')
                and not file_prefix.endswith(os.path.sep)):
            file_prefix += '.'
        for comp_array in self.component_arrays.itervalues():
            # @UndefinedVariable
            comp_array.write_data(file_prefix + comp_array.name + '.pkl',
                                  **kwargs)

    def _get_simulation_params(self, **params):
        sim_params = dict([(p.name, pq.Quantity(p.value, p.unit))
                           for p in self.nineml_model.parameters.values()])
        for key in _REQUIRED_SIM_PARAMS:
            if key in params and params[key]:
                sim_params[key] = params[key]
            elif key not in sim_params or not sim_params[key]:
                raise Exception("'{}' parameter was not specified either in "
                                "Network initialisation or NetworkML "
                                "specification".format(key))
        return sim_params

    @classmethod
    def _flatten_synapse(cls, projection_model):
        """
        Flattens the reponse and plasticity dynamics into a single synapse
        element (will be 9MLv2 format) and updates the port connections
        to match the changed object.
        """
        role2name = {'response': 'psr', 'plasticity': 'pls'}
        syn_comps = {
            role2name['response']: projection_model.response,
            role2name['plasticity']: projection_model.plasticity}
        # Get all projection port connections that don't project to/from
        # the "pre" population and convert them into local MultiDynamics
        # port connections of the synapse
        syn_internal_conns = (
            pc.__class__(
                sender_name=role2name[pc.sender_role],
                receiver_name=role2name[pc.receiver_role],
                send_port=pc.send_port_name, receive_port=pc.receive_port_name)
            for pc in projection_model.port_connections
            if (pc.sender_role in ('plasticity', 'response') and
                pc.receiver_role in ('plasticity', 'response')))
        receive_conns = [pc for pc in projection_model.port_connections
                         if (pc.sender_role in ('pre', 'post') and
                             pc.receiver_role in ('plasticity', 'response'))]
        send_conns = [pc for pc in projection_model.port_connections
                      if (pc.sender_role in ('plasticity', 'response') and
                          pc.receiver_role in ('pre', 'post'))]
        syn_exps = chain(
            (BasePortExposure.from_port(pc.send_port,
                                        role2name[pc.sender_role])
             for pc in send_conns),
            (BasePortExposure.from_port(pc.receive_port,
                                        role2name[pc.receiver_role])
             for pc in receive_conns))
        synapse = MultiDynamicsProperties(
            name=(projection_model.name + '_syn'),
            sub_components=syn_comps,
            port_connections=syn_internal_conns,
            port_exposures=syn_exps)
        port_connections = list(chain(
            (pc.__class__(sender_role=pc.sender_role,
                          receiver_role='synapse',
                          send_port=pc.send_port_name,
                          receive_port=append_namespace(
                              pc.receive_port_name,
                              role2name[pc.receiver_role]))
             for pc in receive_conns),
            (pc.__class__(sender_role='synapse',
                          receiver_role=pc.receiver_role,
                          send_port=append_namespace(
                              pc.send_port_name,
                              role2name[pc.sender_role]),
                          receive_port=pc.receive_port_name)
             for pc in send_conns),
            (pc for pc in projection_model.port_connections
             if (pc.sender_role in ('pre', 'post') and
                 pc.receiver_role in ('pre', 'post')))))
        # A bit of a hack in order to bind the port_connections
        dummy_container = namedtuple('DummyContainer', 'pre post synapse')(
            projection_model.pre, projection_model.post, synapse)
        for port_connection in port_connections:
            port_connection.bind(dummy_container, to_roles=True)
        return synapse, port_connections

    @classmethod
    def _flatten_to_arrays_and_conns(cls, network_model):
        """
        Convert populations and projections into component arrays and
        connection groups
        """
        component_arrays = {}
        connection_groups = {}
        for pop in network_model.populations:
            # Get all the projections that project to/from the given population
            receiving = [p for p in network_model.projections if p.post == pop]
            sending = [p for p in network_model.projections if p.pre == pop]
            # Create a dictionary to hold the cell dynamics and any synapse
            # dynamics that can be flattened into the cell dynamics
            # (i.e. linear ones).
            sub_components = {cls.cell_dyn_name: pop.cell}
            # All port connections between post-synaptic cell and linear
            # synapses and port exposures to pre-synaptic cell
            internal_conns = []
            exposures = []
            synapses = []
            connection_properties = []
            if any(p.name == cls.cell_dyn_name for p in receiving):
                raise Pype9RuntimeError(
                    "Cannot handle projections named '{}' (why would you "
                    "choose such a silly name?;)".format(cls.cell_dyn_name))
            for proj in receiving:
                # Flatten response and plasticity into single dynamics class.
                # TODO: this should be no longer necessary when we move to
                # version 2 as response and plasticity elements will be
                # replaced by a synapse element in the standard. It will need
                # be copied at this point though as it is modified
                synapse, proj_conns = cls._flatten_synapse(proj)
                # Get all connections to/from the pre-synaptic cell
                pre_conns = [pc for pc in proj_conns
                             if 'pre' in (pc.receiver_role, pc.sender_role)]
                # Get all connections between the synapse and the post-synaptic
                # cell
                post_conns = [pc for pc in proj_conns if pc not in pre_conns]
                # Mapping of port connection role to sub-component name
                role2name = {'post': cls.cell_dyn_name}
                # If the synapse is non-linear it can be combined into the
                # dynamics of the post-synaptic cell.
                if synapse.component_class.is_linear():
                    role2name['synapse'] = proj.name
                    # Extract "connection weights" (any non-singular property
                    # value) from the synapse properties
#                     proj_props = defaultdict(set)
#                     for prop in synapse.properties:
#                         # SingleValue properties can be set as a constant but
#                         # any that vary between synapses will need to be
#                         # treated as a connection "weight"
#                         if not isinstance(prop.value, SingleValue):
#                             # FIXME: Need to check whether the property is
#                             #        used in this on event and not in the
#                             #        time derivatives or on conditions
#                             for on_event in (synapse.component_class.
#                                              all_on_events()):
#                                 proj_props[on_event.src_port_name].add(prop)
#                     # Add port weights for this projection to combined list
#                     for port, props in proj_props.iteritems():
#                         ns_props = [
#                             Property(append_namespace(p.name, proj.name),
#                                      p.quantity) for p in props]
#                         connection_properties.append(
#                             ConnectionProperty(
#                                 append_namespace(port, proj.name), ns_props))
                    connection_properties = cls._extract_connection_properties(
                        synapse, proj.name)
                    # Add the flattened synapse to the multi-dynamics sub
                    # components
                    sub_components[proj.name] = synapse
                    # Convert port connections between synpase and post-
                    # synaptic cell into internal port connections of a multi-
                    # dynamics object
                    internal_conns.extend(pc.assign_roles(name_map=role2name)
                                          for pc in post_conns)
                    # Expose ports that are needed for the pre-synaptic
                    # connections
#                     exposures.extend(chain(
#                         (BasePortExposure.from_port(
#                             pc.receive_port, role2name[pc.receiver_role])
#                          for pc in proj_conns if pc.sender_role == 'pre'),
#                         (BasePortExposure.from_port(
#                             pc.send_port, role2name[pc.sender_role])
#                          for pc in proj_conns if pc.receiver_role == 'pre')))
                else:
                    # All synapses (of this type) connected to a single post-
                    # synaptic cell cannot be flattened into a single component
                    # of a multi- dynamics object so an individual synapses
                    # must be created for each connection.
                    synapses.append(SynapseProperties(proj.name, synapse,
                                                      post_conns))
                    # Add exposures to the post-synaptic cell for connections
                    # from the synapse
                    exposures.extend(
                        chain(*(pc.expose_ports({'post': cls.cell_dyn_name})
                                for pc in post_conns)))
                # Add exposures for connections to/from the pre synaptic cell
                exposures.extend(
                    chain(*(pc.expose_ports(role2name) for pc in pre_conns)))

#                         (BasePortExposure.from_port(
#                             pc.receive_port, 'cell')
#                          for pc in proj_conns
#                          if (pc.sender_role == 'pre' and
#                              pc.receiver_role == 'post')),
#                         (BasePortExposure.from_port(
#                             pc.send_port, 'cell')
#                          for pc in proj_conns
#                          if (pc.receiver_role == 'pre' and
#                              pc.sender_role == 'post'))))
                role2name['pre'] = cls.cell_dyn_name
                # Create a connection group for each port connection of the
                # projection to/from the pre-synaptic cell
                for port_conn in pre_conns:
                    connection_group_cls = (
                        EventConnGroup9ML if port_conn.communicates == 'event'
                        else AnalogConnGroup9ML)
                    name = ('__'.join((proj.name,
                                       port_conn.sender_role,
                                       port_conn.send_port_name,
                                       port_conn.receiver_role,
                                       port_conn.receive_port_name)))
                    if port_conn.sender_role == 'pre':
                        connectivity = proj.connectivity
                        # If a connection from the pre-synaptic cell the delay
                        # is included
                        # TODO: In version 2 all port-connections will have
                        # their own delays
                        delay = proj.delay
                    else:
                        # If a "reverse connection" to the pre-synaptic cell
                        # the connectivity needs to be inverted
                        connectivity = InversePyNNConnectivity(
                            proj.connectivity)
                        delay = 0.0 * un.s
                    # Append sub-component namespaces to the source/receive
                    # ports
                    ns_port_conn = port_conn.assign_roles(
                        port_namespaces=role2name)
                    conn_group = connection_group_cls(
                        name,
                        proj.pre.name, proj.post.name,
                        source_port=ns_port_conn.send_port_name,
                        destination_port=ns_port_conn.receive_port_name,
                        connectivity=connectivity,
                        delay=delay)
                    connection_groups[conn_group.name] = conn_group
            # Add exposures for connections to/from the pre-synaptic cell in
            # populations.
            for proj in sending:
                # Not required after transition to version 2 syntax
                synapse, proj_conns = cls._flatten_synapse(proj)
                exposures.extend(chain(*(
                    pc.expose_ports({'pre': cls.cell_dyn_name})
                    for pc in proj_conns)))
            dynamics_properties = MultiDynamicsProperties(
                name=pop.name, sub_components=sub_components,
                port_connections=internal_conns, port_exposures=exposures)
            component = DynamicsWithSynapsesProperties(
                dynamics_properties, synapse_properties=synapses,
                connection_properties=connection_properties)
            component_arrays[pop.name] = ComponentArray9ML(pop.name, pop.size,
                                                           component)
        return component_arrays, connection_groups

    @classmethod
    def _extract_connection_properties(cls, dynamics_properties, namespace):
        """
        Checks the mapping of event port -> analog receive ports to see whether
        the analog receive ports can be treated as a weight of the event port
        (i.e. are not referenced anywhere except within the OnEvent blocks
        triggered by the event port).
        """
        component_class = dynamics_properties.component_class
        varying_props = [
            p for p in dynamics_properties.properties
            if p.value.nineml_type == 'SingleValue']
        # Get list of ports refereneced (either directly or indirectly) by
        # time derivatives and on-conditions
        not_permitted = set(p.name for p in component_class.required_for(
            chain(component_class.all_time_derivatives(),
                  component_class.all_on_conditions())).parameters)
        intersection = set(p.name for p in varying_props) & not_permitted
        if intersection:
            raise Pype9UnflattenableException(intersection)
        conn_params = defaultdict(set)
        for on_event in component_class.on_events:
            on_event_params = component_class.required_for(on_event).parameters
            conn_params[on_event.src_port_name] |= set(on_event_params)
        return [
            ConnectionProperty(
                append_namespace(prt, namespace),
                [Property(append_namespace(p.name, namespace),
                          dynamics_properties.property(p.name).quantity)
                 for p in params])
            for prt, params in conn_params.iteritems()]

#             raise NotImplementedError(
#                 "Cannot convert population '{}' to component array as "
#                 "it has a non-linear synapse or multiple non-single "
#                 "properties")

#         # Get the properties, which are not single values, as they
#         # will have to be varied with each synapse. If there is
#         # only one it the weight of the synapse in NEURON and NEST
#         # can be used to hold it otherwise it won't be possible to
#         # collapse the synapses into a single dynamics object
#         non_single_props = [
#             p for p in synapse.properties
#             if not isinstance(p.value, SingleValue)]


class ComponentArray(object):

    def __init__(self, nineml_model, rng, build_mode='lazy', **kwargs):
        if not isinstance(nineml_model, ComponentArray):
            raise Pype9RuntimeError(
                "Expected a component array, found {}".format(nineml_model))
        dynamics = nineml_model.dynamics
        celltype = self.PyNNCellWrapperClass.__init__(
            dynamics, nineml_model.name, build_mode=build_mode, **kwargs)
        if build_mode not in ('build_only', 'compile_only'):
            cellparams = {}
            initial_values = {}
            for prop in chain(dynamics.properties, dynamics.initial_values):
                val = get_pyNN_value(prop, self.UnitHandler, rng)
                if isinstance(prop, Initial):
                    initial_values[prop.name] = val
                else:
                    cellparams[prop.name] = val
            self.PyNNPopulationClass.__init__(
                self, nineml_model.size, celltype, cellparams=cellparams,
                initial_values=initial_values, label=nineml_model.name)


class ConnectionGroup(object):

    def __init__(self, nineml_model, component_arrays, **kwargs):
        (synapse, conns) = component_arrays[nineml_model.destination].synapse(
            nineml_model.name)
        if conns is not None:
            raise NotImplementedError(
                "Nonlinear synapses, as used in '{}' are not currently "
                "supported".format(nineml_model.name))
        if synapse.num_local_properties > 1:
            raise NotImplementedError(
                "Currently only supports one property that varies with each "
                "synapse")
        # Get the only local property that varies with the synapse, assumed to
        # be the synaptic weight
        # FIXME: This will only work if the weight parameter is only used in
        #        the corresponding on-event. This should be checked when
        #        creating the synapse
        weight = get_pyNN_value(next(synapse.local_properties),
                                self.unit_handler, **kwargs)
        delay = get_pyNN_value(nineml_model.delay, self.unit_handler,
                               **kwargs)
        # FIXME: Ignores send_port, assumes there is only one...
        self.PyNNProjectionClass.__init__(
            self,
            source=component_arrays[nineml_model.source],
            target=component_arrays[nineml_model.destination],
            connectivity=nineml_model.connectivity,
            synapse_type=self.SynapseClass(weight=weight, delay=delay),
            receptor_type=nineml_model.receive_port,
            label=nineml_model.name)
