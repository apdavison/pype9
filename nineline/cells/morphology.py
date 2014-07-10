from itertools import groupby, chain
import collections
from copy import deepcopy
import numpy
from lxml import etree
import quantities as pq
import nineml.extensions.biophysical_cells
from nineml.extensions.morphology import (Morphology as Morphology9ml,
                                          Segment as Segment9ml,
                                          ProximalPoint as ProximalPoint9ml,
                                          DistalPoint as DistalPoint9ml,
                                          ParentSegment as ParentSegment9ml,
                                          Classification as Classification9ml,
                                          SegmentClass as SegmentClass9ml,
                                          Member as Member9ml)
from btmorph.btstructs2 import STree2, SNode2, P3D2


class IrreducibleMorphologyException(Exception):
    pass


class Segment(SNode2):

    @classmethod
    def from_9ml(cls, nineml):
        """
        Creates a node from a 9ml description
        """
        seg = cls(nineml.name,
                  numpy.array((nineml.distal.x, nineml.distal.y,
                               nineml.distal.z)),
                  nineml.distal.diameter)
        return seg

    def __init__(self, name, point, diameter, classes=set()):
        super(Segment, self).__super__(name)
        p3d = P3D2(xyz=point, radius=(diameter / 2.0))
        self.set_content({'p3d': p3d, 'classes': classes})

    def to_9ml(self):
        """
        Returns a 9ml version of the node object
        """
        if self.parent:
            proximal = None
            parent = ParentSegment9ml(self.parent.get_index(), 1.0)
        else:
            parent = None
            root = self.get_parent_node()
            proximal = ProximalPoint9ml(root.xyz[0], root.xyz[1], root.xyz[2],
                                        root.radius * 2.0)
        distal = DistalPoint9ml(self.distal[0], self.distal[1], self.distal[2],
                                self.diameter)
        return Segment9ml(self.get_index(), distal, proximal=proximal,
                          parent=parent)

    def name(self):
        return self._index

    @property
    def distal(self):
        return self.get_content()['p3d'].xyz

    @distal.setter
    def distal(self, distal):
        """
        Sets the distal point of the segment shifting all child
        segments by the same displacement (to keep their lengths constant)

        `distal`         -- the point to update the distal endpoint of the
                            segment to [numpy.array(3)]
        """
        disp = distal - self.distal
        for child in self.all_children:
            child.distal += disp
        self.raw_set_distal(distal)

    def raw_set_distal(self, distal):
        """
        Sets the distal point of the segment without shifting child
        segments

        `distal`         -- the point to update the distal endpoint of the
                            segment to [numpy.array(3)]
        """
        self.get_content()['p3d'].xyz = distal

    @property
    def diameter(self):
        return self.get_content()['p3d'].radius * 2.0

    @diameter.setter
    def diameter(self, diameter):
        self.get_content()['p3d'].radius = diameter / 2.0

    @property
    def proximal(self):
        return self.get_parent_node().distal

    @property
    def disp(self):
        return self.distal - self.proximal

    @property
    def length(self):
        return numpy.sqrt(numpy.sum(self.disp ** 2))

    @length.setter
    def length(self, length):
        """
        Sets the length of the segment, shifting the positions of all child
        nodes so that their lengths stay constant

        `length` -- the new length to set the segment to
        """
        seg_disp = self.distal - self.proximal
        orig_length = numpy.sqrt(numpy.sum(seg_disp ** 2))
        seg_disp *= length / orig_length
        self.distal = self.proximal + seg_disp

    @property
    def parent(self):
        parent = self.get_parent_node()
        # Check to see whether the parent of this node is the root node in
        # which case return None or whether it is another segment
        return parent if isinstance(parent, Segment) else None

    @parent.setter
    def parent(self, parent):
        if not self.parent:
            raise Exception("Cannot set the parent of the root node")
        self.set_parent_node(parent)

    @property
    def children(self):
        return self.get_child_nodes()

    @property
    def siblings(self):
        try:
            return [c for c in self.parent.children if c is not self]
        except AttributeError:  # No parent
            return []

    @property
    def all_children(self):
        for child in self.children:
            yield child
            for childs_child in child.all_children:
                yield childs_child

    @property
    def branch_depth(self):
        branch_count = 0
        seg = self
        while seg.parent_ref:
            if seg.siblings:
                branch_count += 1
            seg = seg.parent_ref.segment
        return branch_count

    @property
    def sub_branches(self):
        """
        Iterates through all sub-branches of the current segment, starting at
        the current segment
        """
        seg = self
        branch = [self]
        while len(seg.children) == 1:
            seg = seg.children[0]
            branch.append(seg)
        yield branch
        for child in seg.children:
            for sub_branch in child.sub_branches:
                yield sub_branch

    def branch_start(self):
        """
        Gets the start of the branch (a section of tree without any sub
        branches the current segment lies on
        """
        seg = self
        while seg.parent and not seg.siblings:
            seg = seg.parent
        return seg


class SegmentClass(object):
    """
    A class of segments
    """

    def __init__(self, name, tree):
        self._tree = tree
        self.name = name
        self._attributes = {}

    def __del__(self):
        self.remove_members(self.members)

    @property
    def members(self):
        for seg in self._tree.segments:
            if self in seg.get_contents()['classes']:
                yield seg

    def to_9ml(self):
        return SegmentClass9ml(self.name, [Member9ml(seg.name)
                                           for seg in self.members])

    def add_attribute(self, attribute):
        if attribute.name in self._attributes:
            raise Exception("Attribute named '{}' is already "
                            "associated with this class"
                            .format(attribute.name))
        self._attributes[attribute.name] = attribute
        self._check_for_duplicate_attributes()

    def remove_attribute(self, attribute_name):
        self._attributes.pop(attribute_name)

    def add_members(self, segments):
        """
        Adds the segments to class
        """
        #TODO: should probably check that segments are in the current tree
        #all_segments = list(self.segments)
        for seg in segments:
            seg.get_contents()['classes'].add(self)
        self._check_for_duplicate_attributes()

    def remove_members(self, segments):
        for seg in segments:
            seg.get_contents()['classes'].remove(self)

    def _check_for_duplicate_attributes(self):
        """
        Checks whether any attributes are duplicated in any segment in the
        tree
        """
        # Get the list of classes that overlap with the current class
        overlapping_classes = reduce(set.union,
                                     [seg.classes for seg in self.members])
        for class1 in overlapping_classes:
            for class2 in overlapping_classes:
                dups = class1._attributes.keys() & class2._attributes.keys()
                if class1 != class2 and dups:
                    segments = [seg for seg in self._tree.segments
                                if (class1 in seg.classes and
                                    class2 in seg.classes)]
                    raise Exception("'{}' attributes clash in segments '{}'{} "
                                    "because of dual membership of classes "
                                    "{} and {}"
                                    .format(dups, segments[:10],
                                            (',...' if len(segments) > 10
                                                    else ''),
                                            class1.name, class2.name))


class Tree(STree2):

    @classmethod
    def from_9ml(cls, nineml):
        tree = cls(nineml.name)
        # Add the proximal point of the root segment as the root of the tree
        root = P3D2(xyz=numpy.array((nineml.root_segment.proximal.x,
                                     nineml.root_segment.proximal.y,
                                     nineml.root_segment.proximal.z)),
                    radius=nineml.root_segment.diameter / 2.0)
        tree.set_root(root)
        # Add the root segment and link with root node
        tree.root_segment = Segment.from_9ml(nineml.root_segment)
        tree.add_node_with_parent(tree.root_segment, tree.get_root())
        seg_lookup = {tree.root_segment.name: tree.root_segment}
        # Initially create all the segments and add them to a lookup dictionary
        for seg_9ml in nineml.segments.itervalues():
            seg_lookup[seg_9ml.name] = Segment.from_9ml(seg_9ml)
        # Then link together all the parents and children
        for seg_9ml in nineml.segments.itervalues():
            tree.add_node_with_parent(seg_lookup[seg_9ml.name],
                                      seg_lookup[seg_9ml.parent.segment_name])
        tree.segment_classes = {}
        for classification in nineml.classifications.itervalues():
            for class_name in classification.classes.itervalues():
                seg_class = tree.add_segment_class(class_name)
                for member in seg_class.members:
                    seg_lookup[member.segment_name].get_contents()['classes'].\
                                                                 add(seg_class)
                tree.segment_classes.append(seg_class)
        return tree

    def __init__(self, name):
        self.name = name

    def to_9ml(self):
        clsf = Classification9ml('default', [c.to_9ml()
                                             for c in self.seg_classes])
        return Morphology9ml(self.name,
                             [seg.to_9ml() for seg in self.segments],
                             {'default': clsf})

    def add_segment_class(self, name):
        """
        Adds a new segment class
        """
        self.segment_classes[name] = seg_class = SegmentClass(name, self)
        return seg_class

    def remove_segment_class(self, name):
        """
        Removes segment class from the classes list of all its members
        and deletes the class
        """
        seg_class = self.segment_classes[name]
        seg_class.remove_members(seg_class.members)
        del self.segment_classes[name]

    @property
    def segments(self):
        """
        Segments are not stored directly as a flat list to allow branches
        to be edited by altering the children of segments. This iterator is
        then used to flatten the list of segments
        """
        return chain([self.root_segment], self.root_segment.all_children)

    @property
    def branches(self):
        """
        An iterator over all branches in the tree
        """
        return self.root.sub_branches

    @property
    def seg_classes(self):
        seg_classes = set()
        for seg in self.segments:
            seg_classes.update(seg.seg_classes)
        return seg_classes

    def segment(self, name):
        match = [seg for seg in self.segments if seg.name == name]
        #TODO: Need to check this on initialisation
        assert len(match) <= 1, "Multiple segments with key '{}'".format(name)
        if not len(match):
            raise KeyError("Segment '{}' was not found".format(name))
        return match[0]

    def merge_leaves(self, only_most_distal=False, normalise_sampling=True):
        """
        Reduces a 9ml morphology, starting at the most distal branches and
        merging them with their siblings.
        """
        # Create a complete copy of the morphology to allow it to be reduced
        if only_most_distal:
            # Get the branches at the maximum depth
            max_branch_depth = max(seg.branch_depth for seg in self.segments)
            candidates = [branch for branch in self.branches
                          if branch[0].branch_depth == max_branch_depth]
        else:
            candidates = [branch for branch in self.branches
                          if not branch[-1].children]
        # Only include branches that have consistent seg_classes
        candidates = [branch for branch in candidates
                      if all(b.seg_classes == branch[0].seg_classes
                             for b in branch)]
        if not candidates:
            raise IrreducibleMorphologyException("Cannot reduce the morphology"
                                                 " further{}. without merging "
                                                 "seg_classes")
        sibling_seg_classes = groupby(candidates,
                                 key=lambda b: (b[0].parent, b[0].seg_classes))
        for (parent, seg_classes), siblings_iter in sibling_seg_classes:
            siblings = list(siblings_iter)
            if len(siblings) > 1:
                average_length = (numpy.sum(seg.length
                                            for seg in chain(*siblings)) /
                                  len(siblings))
                total_surface_area = numpy.sum(seg.length * seg.diameter()
                                               for seg in chain(*siblings))
                diameter = total_surface_area / average_length
                name = siblings[0][0].name
                if len(branch) > 1:
                    name += '_' + siblings[-1][0].name
                # Extend the new segment in the same direction as the parent
                # segment
                disp = parent.disp * (average_length / parent.length)
                segment = Segment(name, parent.distal + disp, diameter,
                                  seg_classes=seg_classes)
                # Remove old branches from list
                for branch in siblings:
                    self.remove_node(branch[0])
                segment.set_parent_node(parent)
        if normalise_sampling:
            self.normalise_spatial_sampling()

    def normalise_spatial_sampling(self, **d_lambda_kwargs):
        """
        Regrids the spatial sampling of the segments in the tree via NEURON's
        d'lambda rule
        """
        self = deepcopy(self)
        default_params = self.biophysics.components['__NO_COMPONENT__'].\
                                                                     parameters
        Ra = pq.Quantity(float(default_params['Ra'].value),
                         default_params['Ra'].unit)
        cm = pq.Quantity(float(default_params['C_m'].value), 'uF/cm^2')  # default_params['C_m'].unit) @IgnorePep8
        to_replace = []
        for branch in self.morphology.branches:
            parent = branch[0].parent
            if parent:
                branch_length = numpy.sum(seg.length for seg in branch) * pq.um
                diameter = (numpy.sum(seg.diameter() for seg in branch) /
                            len(branch) * pq.um)
                num_segments = self.d_lambda_rule(branch_length, diameter, Ra,
                                                  cm, **d_lambda_kwargs)
                base_name = branch[0].name
                if len(branch) > 1:
                    base_name += '_' + branch[-1].name
                # Get the direction of the branch
                seg_classes = branch[0].seg_classes
                direction = branch[-1].distal - branch[0].proximal
                direction *= (branch_length /
                              numpy.sqrt(numpy.sum(direction ** 2)))
                # Temporarily add the parent to the new_branch to allow it to
                # be linked to the new segments
                first_segment = None
                previous_segment = None
                for i, seg_length in enumerate(
                                        numpy.linspace(0.0,
                                                       float(branch_length),
                                                       num_segments)):
                    name = base_name + '_' + str(i)
                    distal = branch[0].proximal + direction * seg_length
                    segment = Segment(name, distal, diameter,
                                      seg_classes=seg_classes)
                    if not first_segment:
                        first_segment = segment
                    else:
                        previous_segment.add_child(segment)
                    previous_segment = segment
                to_replace.append((parent, branch[0], first_segment))
        for parent, orig_branch_start, new_branch_start in to_replace:
            self.remove_node(orig_branch_start)
            self.add_node_with_parent(new_branch_start, parent)
        return self

    @classmethod
    def d_lambda_rule(cls, length, diameter, Ra, cm, freq=(100.0 * pq.Hz),
                      d_lambda=0.1):
        """
        Calculates the number of segments required for a straight branch
        section so that its segments are no longer than d_lambda x the AC
        length constant at frequency freq in that section.

        See Hines, M.L. and Carnevale, N.T.
           NEURON: a tool for neuroscientists.
           The Neuroscientist 7:123-135, 2001.

        `length`     -- length of the branch section
        `diameter`   -- diameter of the branch section
        `Ra`         -- Axial resistance (Ohm cm)
        `cm`         -- membrane capacitance (uF cm^(-2))
        `freq`       -- frequency at which AC length constant will be computed
                        (Hz)
        `d_lambda`   -- fraction of the wavelength

        Returns:
            The number of segments required for the corresponding fraction of
            the wavelength
        """
        # Calculate the wavelength for the segment
        if isinstance(length, collections.Iterable):
            # FIXME: This (variable diameter d_lambda) is not tested yet
            total_length = numpy.sum(in_units(l, 'um') for l in length)
            lam = 0
            for i, lngth in enumerate(length):
                lam += (in_units(lngth, 'um') /
                        numpy.sqrt(in_units(diameter[i], 'um') +
                                   in_units(diameter[i + 1], 'um')))
            lam *= numpy.sqrt(2) * 1e-5 * numpy.sqrt(4 * numpy.pi *
                                                     in_units(freq, 'Hz') *
                                                     in_units(Ra, 'ohm.cm') *
                                                     in_units(cm, 'uF/cm^2'))
            lambda_f = total_length / lam
        else:
            total_length = in_units(length, 'um')
            lambda_f = 1e5 * numpy.sqrt(in_units(diameter, 'um') /
                                        (4 * numpy.pi * in_units(freq, 'Hz') *
                                         in_units(Ra, 'ohm.cm') *
                                         in_units(cm, 'uF/cm^2')))
        return int((length / (d_lambda * lambda_f) + 0.9) / 2) * 2 + 1

    def merge_morphology_seg_classes(self, from_class, into_class):
        raise NotImplementedError


def in_units(quantity, units):
    """
    Returns the quantity as a float in the given units

    `quantity` -- the quantity to convert [pq.Quantity]
    `units`    -- the units to convert to [pq.Quantity]
    """
    return float(pq.Quantity(quantity, units))


if __name__ == '__main__':
    nineml_file = '/home/tclose/git/kbrain/9ml/neurons/Golgi_Solinas08.9ml'
    models = nineml.extensions.biophysical_cells.parse(nineml_file)
    model = next(models.itervalues())
    tree = Tree.from_9ml(model.morphology)
    model.morphology.merge_leaves()
    model.normalise_spatial_sampling(model)
    etree.ElementTree(model.to_9ml().to_xml()).write(
             '/home/tclose/git/kbrain/9ml/neurons/Golgi_Solinas08-reduced.9ml',
             encoding="UTF-8",
             pretty_print=True,
             xml_declaration=True)