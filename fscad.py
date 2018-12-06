# Copyright 2019 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import adsk.core
import adsk.fusion
import collections
import functools
import math
import traceback
import types
import sys
import uuid

from typing import Iterable, List, Any, Union


def _convert_units(value, convert):
    if value is None:
        return None
    if isinstance(value, adsk.core.Point3D):
        return adsk.core.Point3D.create(convert(value.x), convert(value.y), convert(value.z))
    if isinstance(value, adsk.core.Vector3D):
        return adsk.core.Vector3D.create(convert(value.x), convert(value.y), convert(value.z))
    if isinstance(value, tuple):
        return tuple(map(convert, value))
    if isinstance(value, list):
        return list(map(convert, value))
    return convert(value)


def _mm(cm_value):
    return _convert_units(cm_value, lambda value: value if value is None else value * 10)


def _cm(mm_value):
    return _convert_units(mm_value, lambda value: value if value is None else value / 10)


def app():
    return adsk.core.Application.get()


def root() -> adsk.fusion.Component:
    return design().rootComponent


def ui():
    return app().userInterface


def brep():
    return adsk.fusion.TemporaryBRepManager.get()


def design():
    return adsk.fusion.Design.cast(app().activeProduct)


def user_interface():
    return app().userInterface


def _group_timeline(func):
    @functools.wraps(func)
    def func_wrapper(*args, **kwargs):
        initial_count = design().timeline.count
        ret = func(*args, **kwargs)
        timeline_object = None

        index = initial_count + 1
        groups = []
        for index in range(initial_count+1, design().timeline.count):
            item = design().timeline[index]
            if design().timeline[index].isGroup:
                groups.append(item)
        for group in groups:
            group.deleteMe(False)

        if design().timeline.count - initial_count > 1:
            timeline_object = design().timeline.timelineGroups.add(initial_count, design().timeline.count-1)
        elif design().timeline.count - initial_count == 1:
            timeline_object = design().timeline.item(design().timeline.count - 1)
        if timeline_object is not None:
            if "name" in kwargs:
                timeline_object.name = "%s: %s" % (func.__name__, kwargs["name"])
            elif isinstance(ret, adsk.fusion.Occurrence):
                timeline_object.name = "%s: %s" % (func.__name__, ret.name)
            elif len(args) > 0 and isinstance(args[0], adsk.fusion.Occurrence):
                timeline_object.name = "%s: %s" % (func.__name__, args[0].name)
            else:
                timeline_object.name = func.__name__
        return ret
    return func_wrapper


def _collection_of(collection):
    object_collection = adsk.core.ObjectCollection.create()
    for obj in collection:
        object_collection.add(obj)
    return object_collection


def _get_parent_component(occurrence):
    if occurrence.assemblyContext is None:
        return root()
    return occurrence.assemblyContext.component


def _assembly_occurrence(occurrence):
    if occurrence.assemblyContext is not None:
        return occurrence
    occurrences = occurrence.sourceComponent.allOccurrencesByComponent(occurrence.component)
    assert(len(occurrences) == 1)
    return occurrences[0]


def _for_all_child_occurrences(occurrence, func, include_hidden=False):
    func(occurrence)
    for child_occurrence in occurrence.childOccurrences:
        if include_hidden or child_occurrence.isLightBulbOn:
            _for_all_child_occurrences(child_occurrence, func, include_hidden)


def _occurrence_bodies(occurrence: adsk.fusion.Occurrence, include_hidden=False):
    bodies = []
    _for_all_child_occurrences(
        occurrence, lambda child_occurrence: bodies.extend(child_occurrence.bRepBodies), include_hidden)
    return bodies


def _check_2D(occurrence):
    has2D = False
    has3D = False
    for body in _occurrence_bodies(occurrence):
        if body.isSolid:
            has3D = True
        else:
            has2D = True
        if has2D and has3D:
            raise ValueError("Occurrence %s contains both 2D and 3D geometry" % occurrence.name)
    return has2D


def _check_coplanarity(plane1, plane2):
    if plane1 is None or plane2 is None:
        return plane1 or plane2
    if not plane1.isCoPlanarTo(plane2):
        raise ValueError("Cannot perform operation on non-coplanar 2D geometery")
    return plane1 or plane2


def _oriented_bounding_box_to_bounding_box(oriented: adsk.core.OrientedBoundingBox3D):
    return adsk.core.BoundingBox3D.create(
        adsk.core.Point3D.create(
            oriented.centerPoint.x - oriented.length / 2.0,
            oriented.centerPoint.y - oriented.width / 2.0,
            oriented.centerPoint.z - oriented.height / 2.0),
        adsk.core.Point3D.create(
            oriented.centerPoint.x + oriented.length / 2.0,
            oriented.centerPoint.y + oriented.width / 2.0,
            oriented.centerPoint.z + oriented.height / 2.0)
    )


def _get_exact_bounding_box(entity):
    vector1 = adsk.core.Vector3D.create(1.0, 0.0, 0.0)
    vector2 = adsk.core.Vector3D.create(0.0, 1.0, 0.0)

    if isinstance(entity, adsk.fusion.Occurrence):
        entities = _occurrence_bodies(entity)
    else:
        entities = [entity]

    bounding_box = None
    for entity in entities:
        body_bounding_box = _oriented_bounding_box_to_bounding_box(
            app().measureManager.getOrientedBoundingBox(entity, vector1, vector2))
        if bounding_box is None:
            bounding_box = body_bounding_box
        else:
            bounding_box.combine(body_bounding_box)
    return bounding_box


def _create_component(parent_component, *bodies, name):
    new_occurrence = parent_component.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    new_occurrence.component.name = name
    base_feature = new_occurrence.component.features.baseFeatures.add()
    base_feature.startEdit()
    for body in bodies:
        new_occurrence.component.bRepBodies.add(body, base_feature)
    base_feature.finishEdit()
    return new_occurrence


def _mark_face(face, face_name):
    if face.nativeObject is not None:
        face = face.nativeObject
    face_uuid = uuid.uuid4()
    face.attributes.add("fscad", "id", str(face_uuid))
    face.attributes.add("fscad", str(face_uuid), str(face_uuid))
    face.body.attributes.add("fscad", face_name, str(face_uuid))


def _mark_body_and_all_faces(body: adsk.fusion.BRepBody):
    if body.nativeObject is not None:
        body = body.nativeObject
    if body.attributes.itemByName("fscad", "id") is None:
        body_uuid = str(uuid.uuid4())
        body.attributes.add("fscad", "id", body_uuid)
        body.attributes.add("fscad", body_uuid, body_uuid)

    for face in body.faces:
        if face.attributes.itemByName("fscad", "id") is None:
            face_uuid = str(uuid.uuid4())
            face.attributes.add("fscad", "id", face_uuid)
            face.attributes.add("fscad", face_uuid, face_uuid)

@_group_timeline
def sphere(radius, *, name="Sphere") -> adsk.fusion.Occurrence:
    sphere_body = brep().createSphere(adsk.core.Point3D.create(0, 0, 0), _cm(radius))
    occurrence = _create_component(root(), sphere_body, name=name)
    _mark_face(occurrence.component.bRepBodies.item(0).faces.item(0), "surface")

    return occurrence


@_group_timeline
def cylinder(height, radius, radius2=None, *, name="Cylinder") -> adsk.fusion.Occurrence:
    (height, radius, radius2) = _cm((height, radius, radius2))
    cylinder_body = brep().createCylinderOrCone(
        adsk.core.Point3D.create(0, 0, 0),
        radius,
        adsk.core.Point3D.create(0, 0, height),
        radius if radius2 is None else radius2
    )
    occurrence = _create_component(root(), cylinder_body, name=name)
    for face in occurrence.component.bRepBodies.item(0).faces:
        if face.geometry.surfaceType == adsk.core.SurfaceTypes.CylinderSurfaceType or \
                face.geometry.surfaceType == adsk.core.SurfaceTypes.ConeSurfaceType:
            _mark_face(face, "side")
        elif face.geometry.origin.z == 0:
            _mark_face(face, "bottom")
        else:
            _mark_face(face, "top")

    return occurrence


@_group_timeline
def box(x, y, z, *, name="Box") -> adsk.fusion.Occurrence:
    x, y, z = _cm((x, y, z))
    box_body = brep().createBox(adsk.core.OrientedBoundingBox3D.create(
        adsk.core.Point3D.create(x/2, y/2, z/2),
        adsk.core.Vector3D.create(1, 0, 0),
        adsk.core.Vector3D.create(0, 1, 0),
        x, y, z))
    occurrence = _create_component(root(), box_body, name=name)

    def _find_and_mark_face(face_name, _x, _y, _z):
        face = occurrence.component.findBRepUsingPoint(
            adsk.core.Point3D.create(_x, _y, _z),
            adsk.fusion.BRepEntityTypes.BRepFaceEntityType)
        face = face.item(0)
        _mark_face(face, face_name)

    _find_and_mark_face("bottom", x/2, y/2, 0)
    _find_and_mark_face("top", x/2, y/2, z)
    _find_and_mark_face("left", 0, y/2, z/2)
    _find_and_mark_face("right", x, y/2, z/2)
    _find_and_mark_face("front", x/2, 0, z/2)
    _find_and_mark_face("back", x/2, y, z/2)

    return occurrence


@_group_timeline
def rect(x, y, *, name="Rectangle"):
    (x, y) = _cm((x, y))
    curves = [
        adsk.core.Line3D.create(
            adsk.core.Point3D.create(0, 0, 0),
            adsk.core.Point3D.create(x, 0, 0)
        ),
        adsk.core.Line3D.create(
            adsk.core.Point3D.create(x, 0, 0),
            adsk.core.Point3D.create(x, y, 0)
        ),
        adsk.core.Line3D.create(
            adsk.core.Point3D.create(x, y, 0),
            adsk.core.Point3D.create(0, y, 0)
        ),
        adsk.core.Line3D.create(
            adsk.core.Point3D.create(0, y, 0),
            adsk.core.Point3D.create(0, 0, 0)
        )
    ]
    wire, _ = brep().createWireFromCurves(curves)
    face = brep().createFaceFromPlanarWires([wire])

    return _create_component(root(), face, name=name)


@_group_timeline
def circle(r, *, name="Circle"):
    circle = adsk.core.Circle3D.createByCenter(
        adsk.core.Point3D.create(0, 0, 0),
        adsk.core.Vector3D.create(0, 0, 1),
        _cm(r)
    )
    wire, _ = brep().createWireFromCurves([circle])
    face = brep().createFaceFromPlanarWires([wire])

    return _create_component(root(), face, name=name)


@_group_timeline
def extrude(occurrence, height, angle=0, name="Extrude"):
    if not _check_2D(occurrence):
        raise ValueError("Can't use 3D geometry with extrude")
    if not _check_coplanarity(None, occurrence):
        raise ValueError("Can't use non-coplanar 2D geometry with extrude")

    if not occurrence.isLightBulbOn:
        occurrence = _duplicate_occurrence(occurrence, root())
        occurrence.isLightBulbOn = True

    faces = []
    for body in _occurrence_bodies(occurrence):
        faces.extend(body.faces)
    extrude_input = root().features.extrudeFeatures.createInput(
        _collection_of(faces), adsk.fusion.FeatureOperations.NewComponentFeatureOperation)
    extrude_input.setOneSideExtent(
        adsk.fusion.DistanceExtentDefinition.create(adsk.core.ValueInput.createByReal(_cm(height))),
        adsk.fusion.ExtentDirections.PositiveExtentDirection,
        adsk.core.ValueInput.createByReal(math.radians(angle)))
    feature = root().features.extrudeFeatures.add(extrude_input)

    result_occurrence = root().allOccurrencesByComponent(feature.parentComponent)[0]

    for face in feature.startFaces:
        _mark_face(face, "start")
    for face in feature.endFaces:
        _mark_face(face, "end")
    for face in feature.sideFaces:
        _mark_face(face, "side")

    occurrence.moveToComponent(result_occurrence)
    occurrence.createForAssemblyContext(result_occurrence).isLightBulbOn = False
    result_occurrence.component.name = name

    return result_occurrence


def edges(*args):
    if len(args) == 3:
        # provides a more concise way of doing edges(faces(obj, "something"), faces(obj, "else"))
        # e.g. edges(obj, ["something"], ["else"])
        entity = args[0]
        faces1 = faces(entity, *args[1])
        faces2 = faces(entity, *args[2])
        return edges(faces1, faces2)
    else:
        faces1 = args[0]
        faces2 = args[1]
        id_map = {}
        for face1 in faces1:
            for edge1 in face1.edges:
                for edge_face in edge1.faces:
                    if edge_face != face1 and edge_face in faces2:
                        id_edges = id_map.get(edge1.tempId)
                        if id_edges is None:
                            id_edges = []
                            id_map[edge1.tempId] = id_edges
                        if edge1 not in id_edges:
                            id_edges.append(edge1)
        result = []
        for id_edges in id_map.values():
            result.extend(id_edges)
        return result


def all_faces(occurrence: adsk.fusion.Occurrence, *faces: Union[adsk.fusion.BRepFace, Iterable[adsk.fusion.BRepFace]])\
        -> Iterable[adsk.fusion.BRepFace]:
    """Finds all duplicates of the given faces.

    When an occurrence is duplicated via the duplicate function, this allows you to find faces on the duplicated
    occurrences, based on faces from the original occurrence.

    :param occurrence: If specified, filter the returned faces to only those contained in this occurrence
    :param faces: The faces to search for
    """
    result = []
    for face in faces:
        if isinstance(face, Iterable):
            result.extend(all_faces(occurrence, *face))
        else:
            native_face = face
            if face.nativeObject is not None:
                native_face = face.nativeObject
            id_attr = native_face.attributes.itemByName("fscad", "id")
            if id_attr is None:
                raise ValueError("face does not have an id attribute")
            for attr in design().findAttributes("fscad", id_attr.value):
                occurrences = root().allOccurrencesByComponent(attr.parent.body.parentComponent)
                assert occurrences.count == 1
                result.append(attr.parent.createForAssemblyContext(occurrences[0]))
    return result


def faces(entity, *selectors):
    if isinstance(entity, adsk.fusion.Occurrence):
        result = []
        for body in entity.component.bRepBodies:
            for face in faces(body, *selectors):
                result.append(face.createForAssemblyContext(entity))
        return result
    if isinstance(entity, adsk.fusion.BRepBody):
        result = []
        for selector in selectors:
            if isinstance(selector, str):
                attr = entity.attributes.itemByName("fscad", selector)
                if not attr:
                    raise ValueError("Couldn't find face with given name: %s" % selector)
                attributes = design().findAttributes("fscad", attr.value)
                for attribute in attributes:
                    if attribute.parent.body == entity:
                        result.append(attribute.parent)
            elif isinstance(selector, adsk.fusion.BRepFace):
                result.extend(_find_coincident_faces_on_body(entity, selector))
            elif isinstance(selector, collections.Iterable):
                result.extend(faces(entity, *selector))
        return result
    raise ValueError("Unsupported object type: %s" % type(entity).__name__)


def get_face(entity, selector) -> adsk.fusion.BRepFace:
    result = faces(entity, selector)
    if len(result) > 1:
        raise ValueError("Found multiple faces")
    return result[0]


def _check_face_intersection(face1, face2):
    facebody1 = brep().copy(face1)
    facebody2 = brep().copy(face2)
    brep().booleanOperation(facebody1, facebody2, adsk.fusion.BooleanTypes.IntersectionBooleanType)
    return facebody1.faces.count > 0


def _point_vector_to_line(point, vector):
    return adsk.core.Line3D.create(point,
                                   adsk.core.Point3D.create(point.x + vector.x,
                                                            point.y + vector.y,
                                                            point.z + vector.z))

def _check_face_geometry(face1, face2):
    """Does some quick sanity checks of the face geometry, to rule out easy cases of non-equality.

    A return value of True does not guarantee the geometry is the same, but a return value of False does
    guarantee they are not.
    """
    geometry1 = face1.geometry
    geometry2 = face2.geometry
    if isinstance(geometry1, adsk.core.Cylinder):
        if not math.isclose(geometry1.radius, geometry2.radius):
            return False
        line1 = _point_vector_to_line(geometry1.origin, geometry1.axis)
        line2 = _point_vector_to_line(geometry2.origin, geometry2.axis)
        return line1.isColinearTo(line2)
    if isinstance(geometry1, adsk.core.Sphere):
        if not math.isclose(geometry1.radius, geometry2.radius):
            return False
        return geometry1.origin.isEqualTo(geometry2.origin)
    if isinstance(geometry1, adsk.core.Torus):
        if not geometry1.origin.isEqualTo(geometry2.origin):
            return False
        if not geometry1.axis.isParallelTo(geometry2.axis):
            return False
        if not math.isclose(geometry1.majorRadius, geometry2.majorRadius):
            return False
        return math.isclose(geometry1.minorRadius, geometry2.minorRadius)
    if isinstance(geometry1, adsk.core.EllipticalCylinder):
        line1 = _point_vector_to_line(geometry1.origin, geometry1.axis)
        line2 = _point_vector_to_line(geometry2.origin, geometry2.axis)
        if not line1.isColinearTo(line2):
            return False
        if not geometry1.majorAxis.isParallelTo(geometry2.majorAxis):
            return False
        if not math.isclose(geometry1.majorRadius, geometry2.majorRadius):
            return False
        return math.isclose(geometry1.minorRadius, geometry2.minorRadius)
    # It's a bit harder to check the remaining types. We'll just fallback to doing the
    # full face intersection check.
    return True


def _check_face_coincidence(face1, face2):
    if face1.geometry.surfaceType != face2.geometry.surfaceType:
        return False
    if face1.geometry.surfaceType == adsk.core.SurfaceTypes.PlaneSurfaceType:
        if not face1.geometry.isCoPlanarTo(face2.geometry):
            return False
        return _check_face_intersection(face1, face2)
    else:
        if not _check_face_geometry(face1, face2):
            return False
        return _check_face_intersection(face1, face2)


def _find_coincident_faces_on_body(body, *faces):
    coincident_faces = []
    for face in faces:  # type: adsk.fusion.BRepFace
        face_bounding_box = face.boundingBox
        expanded_bounding_box = adsk.core.BoundingBox3D.create(
            adsk.core.Point3D.create(
                face_bounding_box.minPoint.x - app().pointTolerance,
                face_bounding_box.minPoint.y - app().pointTolerance,
                face_bounding_box.minPoint.z - app().pointTolerance),
            adsk.core.Point3D.create(
                face_bounding_box.maxPoint.x + app().pointTolerance,
                face_bounding_box.maxPoint.y + app().pointTolerance,
                face_bounding_box.maxPoint.z + app().pointTolerance),
        )
        if body.boundingBox.intersects(expanded_bounding_box):
            for body_face in body.faces:
                if body_face.boundingBox.intersects(expanded_bounding_box):
                    if _check_face_coincidence(face, body_face):
                        coincident_faces.append(body_face)
    return coincident_faces


def find_coincident_faces(occurrence, *faces):
    """ Find all faces of any visible body in occurrence that are coincident to at least one of the faces in faces """
    coincident_faces = []
    for body in _occurrence_bodies(occurrence):
        coincident_faces.extend(_find_coincident_faces_on_body(body, *faces))

    return coincident_faces


@_group_timeline
def fillet(edges, radius, blend_corners=False):
    if len(edges) == 0:
        return
    component = edges[0].body.parentComponent
    fillet_input = component.features.filletFeatures.createInput()
    fillet_input.addConstantRadiusEdgeSet(_collection_of(edges),
                                          adsk.core.ValueInput.createByReal(_cm(radius)),
                                          False)
    fillet_input.isRollingBallCorner = not blend_corners
    component.features.filletFeatures.add(fillet_input)


@_group_timeline
def chamfer(edges, distance, distance2=None):
    if len(edges) == 0:
        return
    component = edges[0].body.parentComponent
    chamfer_input = component.features.chamferFeatures.createInput(
        _collection_of(edges), False)
    if distance2 is not None:
        chamfer_input.setToTwoDistances(
            adsk.core.ValueInput.createByReal(_cm(distance)),
            adsk.core.ValueInput.createByReal(_cm(distance2)))
    else:
        chamfer_input.setToEqualDistance(
            adsk.core.ValueInput.createByReal(_cm(distance)))

    component.features.chamferFeatures.add(chamfer_input)


@_group_timeline
def loft(*occurrences, name="Loft"):
    loft_input = root().features.loftFeatures.createInput(adsk.fusion.FeatureOperations.NewComponentFeatureOperation)
    for occurrence in occurrences:
        if not _check_2D(occurrence):
            raise ValueError("Can't use 3D geometry with loft")

        for body in _occurrence_bodies(occurrence):
            faces = list(body.faces)
            if len(faces) > 1:
                raise ValueError("A 2D geometry used for loft must only contain a single face")
            loft_input.loftSections.add(faces[0])

    feature = root().features.loftFeatures.add(loft_input)
    result_occurrence = root().allOccurrencesByComponent(feature.parentComponent)[0]

    _mark_face(feature.startFace, "start")
    _mark_face(feature.endFace, "end")
    for face in feature.sideFaces:
        _mark_face(face, "side")

    for occurrence in occurrences:
        occurrence.moveToComponent(result_occurrence)
        occurrence.createForAssemblyContext(result_occurrence).isLightBulbOn = False
    result_occurrence.component.name = name

    return result_occurrence

@_group_timeline
def scale(occurrence, scale_value, center=None):
    def center_point():
        if center is not None:
            base_feature = occurrence.component.features.baseFeatures.add()
            base_feature.startEdit()
            sketch = occurrence.component.sketches.add(root().xYConstructionPlane)
            result = sketch.sketchPoints.add(adsk.core.Point3D.create(0, 0, 0))
            transform = adsk.core.Matrix3D.create()
            transform.translation = _cm(adsk.core.Vector3D.create(*center))
            sketch.transform = transform
            base_feature.finishEdit()
            sketch.isLightBulbOn = False
            return result
        else:
            return root().originConstructionPoint

    def mirror_transform(x, y, z):
        total_transform = adsk.core.Matrix3D.create()
        translate_transform = adsk.core.Matrix3D.create()
        mirror_transform = adsk.core.Matrix3D.create()
        if center:
            translate_transform.translation = _cm(adsk.core.Vector3D.create(*center))
        if x:
            mirror_transform.setCell(0, 0, -1)
        if y:
            mirror_transform.setCell(1, 1, -1)
        if z:
            mirror_transform.setCell(2, 2, -1)
        translate_transform.invert()
        total_transform.transformBy(translate_transform)
        total_transform.transformBy(mirror_transform)
        translate_transform.invert()
        total_transform.transformBy(translate_transform)
        return total_transform

    def mirror_body(occurrence, body, transform):
        copy = brep().copy(body)
        brep().transform(copy, transform)
        base_feature = root().features.baseFeatures.add()
        base_feature.startEdit()
        copy2 = root().bRepBodies.add(copy, base_feature)
        base_feature.finishEdit()

        base_feature = occurrence.component.features.baseFeatures.add()
        base_feature.startEdit()
        copy = copy2.copyToComponent(occurrence)
        copy2.deleteMe()
        base_feature.finishEdit()

        for attr in body.nativeObject.attributes:
            copy.nativeObject.attributes.add(attr.groupName, attr.name, attr.value)

        original_faces = list(body.faces)
        copy_faces = list(copy.faces)

        assert len(original_faces) == len(copy_faces)
        for i in range(0, len(original_faces)-1):
            for attr in original_faces[i].nativeObject.attributes:
                copy_faces[i].nativeObject.attributes.add(attr.groupName, attr.name, attr.value)

    if isinstance(scale_value, Iterable):
        scale_value = list(scale_value)
        if len(scale_value) != 3:
            raise ValueError("Expecting either a single scale value, or a list/tuple with x/y/z scales")
        if tuple(scale_value) == (1, 1, 1):
            return
        mirror = [False, False, False]
        if scale_value[0] < 0:
            mirror[0] = True
            scale_value[0] = abs(scale_value[0])
        if scale_value[1] < 0:
            mirror[1] = True
            scale_value[1] = abs(scale_value[1])
        if scale_value[2] < 0:
            mirror[2] = True
            scale_value[2] = abs(scale_value[2])

        if tuple(scale_value) != (1, 1, 1):
            scale_input = occurrence.component.features.scaleFeatures.createInput(
                _collection_of(_occurrence_bodies(occurrence, True)),
                center_point(),
                adsk.core.ValueInput.createByReal(1))
            scale_input.setToNonUniform(
                adsk.core.ValueInput.createByReal(scale_value[0]),
                adsk.core.ValueInput.createByReal(scale_value[1]),
                adsk.core.ValueInput.createByReal(scale_value[2]))
            occurrence.component.features.scaleFeatures.add(scale_input)
        if tuple(mirror) != (False, False, False):
            bodies_to_delete = []
            transform = mirror_transform(*mirror)

            def do_mirror(occurrence):
                for body in list(occurrence.bRepBodies):
                    mirror_body(occurrence, body, transform)
                    bodies_to_delete.append(body)
            _for_all_child_occurrences(occurrence, do_mirror, True)

            for body in bodies_to_delete:
                body.parentComponent.features.removeFeatures.add(body)
        return occurrence
    else:
        if scale_value == 1:
            return
        mirror = False
        if scale_value < 0:
            mirror = True
            scale_value = abs(scale_value)

        body_collection = _collection_of(_occurrence_bodies(occurrence, True))
        if scale_value != 1:
            scale_input = occurrence.component.features.scaleFeatures.createInput(
                body_collection,
                center_point(),
                adsk.core.ValueInput.createByReal(scale_value))
            occurrence.component.features.scaleFeatures.add(scale_input)
        if mirror:
            bodies_to_delete = []
            transform = mirror_transform(True, True, True)

            def do_mirror(occurrence):
                for body in list(occurrence.bRepBodies):
                    mirror_body(occurrence, body, transform)
                    bodies_to_delete.append(body)
            _for_all_child_occurrences(occurrence, do_mirror, True)

            for body in bodies_to_delete:
                body.parentComponent.features.removeFeatures.add(body)
        return occurrence


def _do_intersection(target_occurrence, tool_bodies):
    for target_body in _occurrence_bodies(target_occurrence):
        combine_input = target_occurrence.component.features.combineFeatures.createInput(target_body, tool_bodies)
        combine_input.operation = adsk.fusion.FeatureOperations.IntersectFeatureOperation
        combine_input.isKeepToolBodies = True
        target_occurrence.component.features.combineFeatures.add(combine_input)


@_group_timeline
def intersection(*occurrences, name=None):
    base_occurrence = occurrences[0]

    plane = None
    if _check_2D(base_occurrence):
        plane = _get_plane(base_occurrence)

    result_occurrence = _get_parent_component(base_occurrence).occurrences.addNewComponent(adsk.core.Matrix3D.create())
    result_occurrence.component.name = name or base_occurrence.component.name

    for body in _occurrence_bodies(base_occurrence):
        body.copyToComponent(result_occurrence)

    for tool_occurrence in occurrences[1:]:
        if _check_2D(tool_occurrence):
            plane = _check_coplanarity(plane, _get_plane(tool_occurrence))
        _do_intersection(result_occurrence, _collection_of(_occurrence_bodies(tool_occurrence)))

    for occurrence in occurrences:
        occurrence.moveToComponent(result_occurrence)
        occurrence = occurrence.createForAssemblyContext(result_occurrence)
        occurrence.isLightBulbOn = False
    if base_occurrence.assemblyContext is not None:
        result_occurrence = result_occurrence.createForAssemblyContext(base_occurrence.assemblyContext)

    return result_occurrence


def _do_difference(target_occurrence, tool_occurrence):
    tool_bodies = adsk.core.ObjectCollection.create()  # type: adsk.core.ObjectCollection
    for tool_body in _occurrence_bodies(tool_occurrence):
        tool_bodies.add(tool_body)

    for target_body in _occurrence_bodies(target_occurrence):
        combine_input = target_occurrence.component.features.combineFeatures.createInput(target_body, tool_bodies)
        combine_input.operation = adsk.fusion.FeatureOperations.CutFeatureOperation
        combine_input.isKeepToolBodies = True
        target_occurrence.component.features.combineFeatures.add(combine_input)


@_group_timeline
def difference(*occurrences, name=None):
    base_occurrence = occurrences[0]

    is2D = _check_2D(base_occurrence)
    plane = None

    result_occurrence = _get_parent_component(base_occurrence).occurrences.addNewComponent(adsk.core.Matrix3D.create())
    result_occurrence.component.name = name or base_occurrence.component.name
    for body in _occurrence_bodies(base_occurrence):
        if is2D:
            plane = _check_coplanarity(plane, _get_plane(body))
        body.copyToComponent(result_occurrence)

    try:
        for tool_occurrence in occurrences[1:]:
            if is2D:
                if _check_2D(tool_occurrence):
                    for body in _occurrence_bodies(tool_occurrence):
                        _check_coplanarity(plane, _get_plane(body))
            else:
                if _check_2D(tool_occurrence):
                    raise ValueError("Can't subtract 2D geometry from 3D geometry")
            _do_difference(result_occurrence, tool_occurrence)
    except ValueError:
        result_occurrence.deleteMe()
        raise

    for occurrence in occurrences:
        occurrence.moveToComponent(result_occurrence)
        occurrence = occurrence.createForAssemblyContext(result_occurrence)
        occurrence.isLightBulbOn = False
    if base_occurrence.assemblyContext is not None:
        result_occurrence = result_occurrence.createForAssemblyContext(base_occurrence.assemblyContext)

    return result_occurrence


@_group_timeline
def translate(occurrence, x=0, y=0, z=0):
    if x == 0 and y == 0 and z == 0:
        return occurrence

    bodies_to_move = adsk.core.ObjectCollection.create()
    for body in _occurrence_bodies(occurrence):
        bodies_to_move.add(body)

    transform = adsk.core.Matrix3D.create()
    transform.translation = _cm(adsk.core.Vector3D.create(x, y, z))

    original_transform = occurrence.transform  # type: adsk.core.Matrix3D
    original_transform.transformBy(transform)
    occurrence.transform = original_transform
    design().snapshots.add()

    return occurrence


@_group_timeline
def rotate(occurrence, x=0, y=0, z=0, center=None):
    if x == 0 and y == 0 and z == 0:
        return occurrence

    if center is None:
        center = adsk.core.Point3D.create(0, 0, 0)
    else:
        center = _cm(adsk.core.Point3D.create(*center))

    bodies_to_rotate = adsk.core.ObjectCollection.create()
    for body in _occurrence_bodies(occurrence):
        bodies_to_rotate.add(body)

    transform1 = adsk.core.Matrix3D.create()
    transform1.setToRotation(math.radians(x), adsk.core.Vector3D.create(1, 0, 0), center)
    transform2 = adsk.core.Matrix3D.create()
    transform2.setToRotation(math.radians(y), adsk.core.Vector3D.create(0, 1, 0), center)
    transform3 = adsk.core.Matrix3D.create()
    transform3.setToRotation(math.radians(z), adsk.core.Vector3D.create(0, 0, 1), center)

    transform1.transformBy(transform2)
    transform1.transformBy(transform3)

    transform = occurrence.transform  # type: adsk.core.Matrix3D
    transform.transformBy(transform1)
    occurrence.transform = transform
    design().snapshots.add()

    return occurrence


@_group_timeline
def group(*occurrences, name="Group") -> adsk.fusion.Occurrence:
    new_occurrence = root().occurrences.addNewComponent(adsk.core.Matrix3D.create())  # type: adsk.fusion.Occurrence
    new_component = new_occurrence.component  # type: adsk.fusion.Component
    new_component.name = name

    for occurrence in occurrences:
        occurrence.moveToComponent(new_occurrence)

    return new_occurrence


def _get_plane(entity):
    if isinstance(entity, adsk.fusion.BRepBody):
        body = entity
        if body.isSolid:
            raise ValueError("Can't get the plane of a 3D object")
        plane = None
        for face in body.faces:
            if not isinstance(face.geometry, adsk.core.Plane):
                raise ValueError("Can't get the plane of a non-planar face")
            if plane is None:
                plane = face.geometry
            else:
                _check_coplanarity(plane, face.geometry)
        return plane
    else:
        plane = None
        for body in _occurrence_bodies(entity):
            if plane is None:
                plane = _get_plane(body)
            else:
                _check_coplanarity(plane, _get_plane(body))
        return plane


@_group_timeline
def union(*occurrences, name=None):
    is2D = None
    plane = None
    bodies = []
    for occurrence in occurrences:
        if is2D is None:
            is2D = _check_2D(occurrence)
        elif is2D != _check_2D(occurrence):
            raise ValueError("Can't union 2D and 3D geometry")

        if is2D:
            plane = _check_coplanarity(plane, _get_plane(occurrence))

        bodies.extend(_occurrence_bodies(occurrence))

    base_occurrence = occurrences[0]

    parent_component = _get_parent_component(base_occurrence)
    result_occurrence = parent_component.occurrences.addNewComponent(adsk.core.Matrix3D.create())

    if len(bodies) > 1:
        body_copies = []
        for body in bodies:
            body_copies.append(body.copyToComponent(result_occurrence))

        combine_input = parent_component.features.combineFeatures.createInput(
            body_copies[0], _collection_of(body_copies[1:]))
        combine_input.operation = adsk.fusion.FeatureOperations.JoinFeatureOperation
        combine_input.isKeepToolBodies = False
        combine_input.isNewComponent = False
        parent_component.features.combineFeatures.add(combine_input)
        for occurrence in occurrences:
            occurrence.moveToComponent(result_occurrence)
            occurrence = occurrence.createForAssemblyContext(result_occurrence)
            occurrence.isLightBulbOn = False
    else:
        for occurrence in occurrences:
            occurrence.moveToComponent(result_occurrence)
    result_occurrence.component.name = name or base_occurrence.component.name

    if base_occurrence.assemblyContext is not None:
        result_occurrence = result_occurrence.createForAssemblyContext(base_occurrence.assemblyContext)

    return result_occurrence


class Joiner(object):
    def __init__(self, join_method, name=None):
        self._entities = []
        self._name = name
        self._join_method = join_method

    def __enter__(self):
        return self

    def __exit__(self, error_type, value, trace):
        if error_type is None:
            occurrence = self._join_method(*self._entities, name=self._name)
            self._occurrence = occurrence

    def __call__(self, entity):
        # TODO: check that the type matches the existing entities
        # also check that the context is still active
        self._entities.append(entity)
        return entity

    def result(self):
        return self._occurrence


def minOf(occurrence):
    return _mm(_get_exact_bounding_box(occurrence).minPoint)


def maxOf(occurrence):
    return _mm(_get_exact_bounding_box(occurrence).maxPoint)


def midOf(occurrence):
    bounding_box = _get_exact_bounding_box(occurrence)
    return _mm(adsk.core.Point3D.create(
        (bounding_box.minPoint.x + bounding_box.maxPoint.x) / 2,
        (bounding_box.minPoint.y + bounding_box.maxPoint.y) / 2,
        (bounding_box.minPoint.z + bounding_box.maxPoint.z) / 2
    ))


def sizeOf(occurrence):
    bounding_box = _get_exact_bounding_box(occurrence)
    return _mm(adsk.core.Point3D.create(
        bounding_box.maxPoint.x - bounding_box.minPoint.x,
        bounding_box.maxPoint.y - bounding_box.minPoint.y,
        bounding_box.maxPoint.z - bounding_box.minPoint.z
    ))


def _get_placement_value(value, coordinate_index):
    if callable(value):
        return _cm(value(coordinate_index))
    return _cm(value)


def minAt(value):
    return lambda coordinate_index, bounding_box: _mm(
        _get_placement_value(value, coordinate_index) - bounding_box.minPoint.asArray()[coordinate_index])


def maxAt(value):
    return lambda coordinate_index, bounding_box: _mm(
        _get_placement_value(value, coordinate_index) - bounding_box.maxPoint.asArray()[coordinate_index])


def midAt(value):
    return lambda coordinate_index, bounding_box: _mm(
        _get_placement_value(value, coordinate_index) -
        (bounding_box.minPoint.asArray()[coordinate_index] + bounding_box.maxPoint.asArray()[coordinate_index]) / 2)


def atMin(entity):
    bounding_box = _get_exact_bounding_box(entity)
    return lambda coordinate_index: _mm(bounding_box.minPoint.asArray()[coordinate_index])


def atMax(entity):
    bounding_box = _get_exact_bounding_box(entity)
    return lambda coordinate_index: _mm(bounding_box.maxPoint.asArray()[coordinate_index])


def atMid(entity):
    bounding_box = _get_exact_bounding_box(entity)
    return lambda coordinate_index: _mm(
        (bounding_box.minPoint.asArray()[coordinate_index] + bounding_box.maxPoint.asArray()[coordinate_index]) / 2)


def keep():
    return lambda *_: 0


def touching(anchor_occurrence, target_occurrence):
    measure_result = app().measureManager.measureMinimumDistance(target_occurrence, anchor_occurrence)

    translate(target_occurrence, *_mm((
        _mm(measure_result.positionTwo.x - measure_result.positionOne.x),
        _mm(measure_result.positionTwo.y - measure_result.positionOne.y),
        _mm(measure_result.positionTwo.z - measure_result.positionOne.z))))


def distance_between(occurrence1, occurrence2):
    measure_result = app().measureManager.measureMinimumDistance(occurrence1, occurrence2)
    return math.sqrt(
        math.pow(_mm(measure_result.positionTwo.x - measure_result.positionOne.x), 2) +
        math.pow(_mm(measure_result.positionTwo.y - measure_result.positionOne.y), 2) +
        math.pow(_mm(measure_result.positionTwo.z - measure_result.positionOne.z), 2))


def tx(occurrence, translation):
    return translate(occurrence, translation, 0, 0)


def ty(occurrence, translation):
    return translate(occurrence, 0, translation, 0)


def tz(occurrence, translation):
    return translate(occurrence, 0, 0, translation)


def rx(occurrence, angle, center=None):
    return rotate(occurrence, angle, 0, 0, center=center)


def ry(occurrence, angle, center=None):
    return rotate(occurrence, 0, angle, 0, center=center)


def rz(occurrence, angle, center=None):
    return rotate(occurrence, 0, 0, angle, center=center)


def _duplicate_occurrence(occurrence: adsk.fusion.Occurrence, parent_component=None):
    if not parent_component:
        parent_component = _get_parent_component(occurrence)
    parent_occurrence = root().allOccurrencesByComponent(parent_component)
    if parent_occurrence:
        parent_occurrence = parent_occurrence[0]

    component_id_attr = occurrence.component.attributes.itemByName("fscad", "id")
    if occurrence.component.attributes.itemByName("fscad", "id") is None:
        component_uuid = str(uuid.uuid4())
        occurrence.component.attributes.add("fscad", "id", component_uuid)
        occurrence.component.attributes.add("fscad", component_uuid, component_uuid)
    else:
        component_uuid = component_id_attr.value

    result_occurrence = parent_component.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    if parent_occurrence:
        result_occurrence = result_occurrence.createForAssemblyContext(parent_occurrence)
    result_occurrence.component.name = occurrence.component.name
    result_occurrence.isLightBulbOn = occurrence.isLightBulbOn
    result_occurrence.component.attributes.add("fscad", "id", component_uuid)
    result_occurrence.component.attributes.add("fscad", component_uuid, component_uuid)

    for body in occurrence.bRepBodies:
        _mark_body_and_all_faces(body)
        body.copyToComponent(result_occurrence)
    for child_occurrence in occurrence.childOccurrences:
        _duplicate_occurrence(child_occurrence, result_occurrence.component)
    return result_occurrence


def duplicate_of(occurrence):
    dup = _duplicate_occurrence(occurrence, root())
    dup.isLightBulbOn = True
    return dup


@_group_timeline
def duplicate(func, values, occurrence):
    parent_component = _get_parent_component(occurrence)

    result_occurrence = parent_component.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    result_occurrence.component.name = occurrence.component.name
    occurrence.moveToComponent(result_occurrence)
    occurrence = occurrence.createForAssemblyContext(result_occurrence)

    for value in values[1:]:
        duplicate_occurrence = _duplicate_occurrence(occurrence)
        func(duplicate_occurrence, value)

    func(occurrence, values[0])
    return result_occurrence


def find_all_duplicates(occurrence):
    id_attr = occurrence.component.attributes.itemByName("fscad", "id")
    occurrences = []
    if id_attr is not None:
        for attr in design().findAttributes("fscad", id_attr.value):
            occurrences.extend(root().allOccurrencesByComponent(attr.parent))
    else:
        occurrences.append(occurrence)
    return occurrences


@_group_timeline
def place(occurrence, x=keep(), y=keep(), z=keep()) -> adsk.fusion.Occurrence:
    bounding_box = _get_exact_bounding_box(occurrence)
    translate(occurrence,
              x(0, bounding_box),
              y(1, bounding_box),
              z(2, bounding_box))

    return occurrence


def setup_document(document_name="fSCAD-Preview"):
    preview_doc = None  # type: adsk.fusion.FusionDocument
    saved_camera = None
    for document in app().documents:
        if document.name == document_name:
            preview_doc = document
            break
    if preview_doc is not None:
        preview_doc.activate()
        saved_camera = app().activeViewport.camera
        preview_doc.close(False)

    preview_doc = app().documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    preview_doc.name = document_name
    preview_doc.activate()
    if saved_camera is not None:
        is_smooth_transition_bak = saved_camera.isSmoothTransition
        saved_camera.isSmoothTransition = False
        app().activeViewport.camera = saved_camera
        saved_camera.isSmoothTransition = is_smooth_transition_bak
        app().activeViewport.camera = saved_camera


def run_design(design_func, message_box_on_error=True, document_name="fSCAD-Preview"):
    """
    Utility method to handle the common setup tasks for a script

    :param design_func: The function that actually creates the design
    :param message_box_on_error: Set true to pop up a dialog with a stack trace if an error occurs
    :param document_name: The name of the document to create. If a document of the given name already exists, it will
    be forcibly closed and recreated.
    """
    try:
        setup_document(document_name)
        design_func()
    except:
        print(traceback.format_exc())
        if message_box_on_error:
            ui = user_interface()
            if ui:
                ui.messageBox('Failed:\n{}'.format(traceback.format_exc()))


def run(context):
    fscad = types.ModuleType("fscad")
    sys.modules['fscad'] = fscad

    for key, value in globals().items():
        if not callable(value):
            continue
        if key == "run" or key == "stop":
            continue
        fscad.__setattr__(key, value)


def stop(context):
    del sys.modules['fscad']
