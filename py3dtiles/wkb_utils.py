import numpy as np
import math
import struct
from .earcut import earcut


class TriangleSoup:
    def __init__(self):
        self.triangles = []

    @staticmethod
    def from_wkb_multipolygon(wkb, associatedData=[]):
        """
        Parameters
        ----------
        wkb : string
            Well-Known Binary binary string describing a multipolygon

        associatedData : array
            array of multipolygons containing data attached to the wkb
            parameter multipolygon. Must be the same size as wkb.

        Returns
        -------
        ts : TriangleSoup
        """
        multipolygons = [parse(bytes(wkb))]

        for additionalWkb in associatedData:
            multipolygons.append(parse(bytes(additionalWkb)))

        trianglesArray = [[] for _ in range(len(multipolygons))]
        for i in range(0, len(multipolygons[0])):
            polygon = multipolygons[0][i]
            additionalPolygons = [mp[i] for mp in multipolygons[1:]]
            triangles = triangulate(polygon, additionalPolygons)
            for array, tri in zip(trianglesArray, triangles):
                array += tri
            """if(len(polygon) != 1):
                print("No support for inner polygon rings")
            else:
                if(len(polygon[0]) > 3):
                    triangles = triangulate(polygon[0],
                                            [p[0] for p in additionalPolygons])
                    for array, tri in zip(trianglesArray, triangles):
                        array += tri
                else:
                    for array, tri in zip(trianglesArray,
                                          [polygon] + additionalPolygons):
                        array += tri"""

        ts = TriangleSoup()
        ts.triangles = trianglesArray

        return ts

    def getPositionArray(self):
        """
        Parameters
        ----------

        Returns
        -------
        Binary array of vertice positions
        """

        verticeTriangles = self.triangles[0]
        verticeArray = vertexAttributeToArray(verticeTriangles)
        return b''.join(verticeArray)

    def getDataArray(self, index):
        """
        Parameters
        ----------
        index: int
            The index of the associated data

        Returns
        -------
        Binary array of vertice data
        """

        verticeTriangles = self.triangles[1 + index]
        verticeArray = vertexAttributeToArray(verticeTriangles)
        return b''.join(verticeArray)

    def getNormalArray(self):
        """
        Parameters
        ----------

        Returns
        -------
        Binary array of vertice normals
        """
        normals = []
        for t in self.triangles[0]:
            U = t[1] - t[0]
            V = t[2] - t[0]
            N = np.cross(U, V)
            norm = np.linalg.norm(N)
            if norm == 0:
                normals.append(np.array([0, 0, 1], dtype=np.float32))
            else:
                normals.append(N / norm)

        verticeArray = faceAttributeToArray(normals)
        return b''.join(verticeArray)

    def getBbox(self):
        """
        Parameters
        ----------

        Returns
        -------
        Array [[minX, minY, minZ],[maxX, maxY, maxZ]]
        """
        mins = np.array([np.min(t, 0) for t in self.triangles[0]])
        maxs = np.array([np.max(t, 0) for t in self.triangles[0]])
        return [np.min(mins, 0), np.max(maxs, 0)]


def faceAttributeToArray(triangles):
    array = []
    for face in triangles:
        array += [face, face, face]
    return array


def vertexAttributeToArray(triangles):
    array = []
    for face in triangles:
        for vertex in face:
            array.append(vertex)
    return array


def parse(wkb):
    multipolygon = []
    # length = len(wkb)
    # print(length)
    byteorder = struct.unpack('b', wkb[0:1])
    bo = '<' if byteorder[0] else '>'
    geomtype = struct.unpack(bo + 'I', wkb[1:5])[0]
    hasZ = (geomtype == 1006) or (geomtype == 1015)
    # MultipolygonZ or polyhedralSurface
    pntOffset = 24 if hasZ else 16
    pntUnpack = 'ddd' if hasZ else 'dd'
    geomNb = struct.unpack(bo + 'I', wkb[5:9])[0]
    # print(struct.unpack('b', wkb[9:10])[0])
    # print(struct.unpack('I', wkb[10:14])[0])   # 1003 (Polygon)
    # print(struct.unpack('I', wkb[14:18])[0])   # num lines
    # print(struct.unpack('I', wkb[18:22])[0])   # num points
    offset = 9
    for i in range(0, geomNb):
        offset += 5  # struct.unpack('bI', wkb[offset:offset + 5])[0]
        # 1 (byteorder), 1003 (Polygon)
        lineNb = struct.unpack(bo + 'I', wkb[offset:offset + 4])[0]
        offset += 4
        polygon = []
        for j in range(0, lineNb):
            pointNb = struct.unpack(bo + 'I', wkb[offset:offset + 4])[0]
            offset += 4
            line = []
            for k in range(0, pointNb - 1):
                pt = np.array(struct.unpack(bo + pntUnpack, wkb[offset:offset
                              + pntOffset]), dtype=np.float32)
                offset += pntOffset
                line.append(pt)
            offset += pntOffset   # skip redundant point
            polygon.append(line)
        multipolygon.append(polygon)
    return multipolygon


def triangulate(polygon, additionalPolygons=[]):
    """
    Triangulates 3D polygons
    """
    # let's find out if the polygon is *mostly* clockwise or counter-clockwise
    # and triangulate accordingly
    # for 2D explanations:
    # https://stackoverflow.com/a/1165943/1528985
    # and https://www.element84.com/blog/determining-the-winding-of-a-polygon-given-as-a-set-of-ordered-points
    #
    # Quick explanation in case it goes down: for each edge we calculate the
    # area of the polygon formed by this edge, the x axis and the 2 vertical.
    # It's (x2-x1) / ((y2+y1 / 2) (draw it if you don't believe me). This
    # results will be positive for a edge that goes toward positive x.  Summing
    # all these areas will give plus or minus the total polygon area.  it would
    # be positive for a clockwise polygon (upper edges contributing positively)
    # and negative for counter-clockwise polygons (upper edges contributing
    # negatively)
    #
    # Adaptations here:
    # - we prefer to reason with counter-clockwise positive, hence the x1-x2 instead of x2-x1
    # - in 3D, we calcule this value for each axis planes (xy, yz, zx),
    # looking in the other axis negative direction.
    # - comparing these 3 results actually give us the most interesting plane
    # to triangulate in (the plane were the projected area is the biggest)
    # - we drop the 1/2 factor because we are only interesting in the sign and relative comparison
    vectProd = np.array([0, 0, 0], dtype=np.float32)
    for i in range(len(polygon[0])):
        curr_edge = polygon[0][i]
        next_edge = polygon[0][(i + 1) % len(polygon[0])]
        vectProd += np.array([
            # yz plane, seen from negative x
            (curr_edge[1] - next_edge[1]) * (next_edge[2] + curr_edge[2]),
            # zx plane, seen from negative y
            (curr_edge[2] - next_edge[2]) * (next_edge[0] + curr_edge[0]),
            # xy plane, seen from negative z
            (curr_edge[0] - next_edge[0]) * (next_edge[1] + curr_edge[1]),
        ], dtype=np.float32)

    polygon2D = []
    holes = []
    delta = 0
    for p in polygon[:-1]:
        holes.append(delta + len(p))
        delta += len(p)
    # triangulation of the polygon projected on planes (xy) (zx) or (yz)
    if(math.fabs(vectProd[0]) > math.fabs(vectProd[1])
       and math.fabs(vectProd[0]) > math.fabs(vectProd[2])):
        # (yz) projection
        for linestring in polygon:
            for point in linestring:
                polygon2D.extend([point[1], point[2]])
    elif(math.fabs(vectProd[1]) > math.fabs(vectProd[2])):
        # (zx) projection
        for linestring in polygon:
            for point in linestring:
                polygon2D.extend([point[0], point[2]])
    else:
        # (xy) projextion
        for linestring in polygon:
            for point in linestring:
                polygon2D.extend([point[0], point[1]])

    trianglesIdx = earcut(polygon2D, holes, 2)

    arrays = [[] for _ in range(len(additionalPolygons) + 1)]
    for i in range(0, len(trianglesIdx), 3):
        t = trianglesIdx[i:i + 3]
        p0 = unflatten(polygon, holes, t[0])
        p1 = unflatten(polygon, holes, t[1])
        p2 = unflatten(polygon, holes, t[2])
        # triangulation may break triangle orientation, test it before
        # adding triangles
        # FIXME fix / change the triangulation code instead?
        crossProduct = np.cross(p1 - p0, p2 - p0)
        invert = np.dot(vectProd, crossProduct) < 0
        if invert:
            arrays[0].append([p1, p0, p2])
        else:
            arrays[0].append([p0, p1, p2])
        for array, p in zip(arrays[1:], additionalPolygons):
            pp0 = unflatten(p, holes, t[0])
            pp1 = unflatten(p, holes, t[1])
            pp2 = unflatten(p, holes, t[2])
            if invert:
                array.append([pp1, pp0, pp2])
            else:
                array.append([pp0, pp1, pp2])

    return arrays


def unflatten(array, lengths, index):
    for i in reversed(range(0, len(lengths))):
        lgth = lengths[i]
        if index >= lgth:
            return array[i + 1][index - lgth]
    return array[0][index]
