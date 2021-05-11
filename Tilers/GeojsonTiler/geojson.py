# -*- coding: utf-8 -*-
import sys
import numpy as np
import pywavefront
import json
from py3dtiles import BoundingVolumeBox, TriangleSoup
from Tilers.object_to_tile import ObjectToTile, ObjectsToTile

import os
from os import listdir
from os.path import isfile, join


# Todo : Comment
class Geojson(ObjectToTile):
    def __init__(self, id = None):
        super().__init__(id)

        self.geom = TriangleSoup()

    def get_geom_as_triangles(self):
        return self.geom.triangles[0]

    def set_triangles(self,triangles):
        self.geom.triangles[0] = triangles

    def get_center(self,coords):
        x = 0
        y = 0
        z = 0
        
        for i in range(0,len(coords),3):
            x += coords[i]
            y += coords[i + 1]
            z += coords[i + 2]

        x /= len(coords) / 3
        y /= len(coords) / 3
        z /= len(coords) / 3

        return [x, y, z] 

    def create_triangles(self,vertices,coordsLenght):
        triangles = np.zeros(shape=(coordsLenght * 4, 3, 3))
        k = 0

        # Triangles faces haute et basse
        for j in range(1,coordsLenght + 1):
            # Basse
            triangles[k] = [vertices[0], vertices[(j % coordsLenght) + 1], vertices[j]]

            # Haute
            triangles[k + 1] = [vertices[(coordsLenght + 1)], vertices[(coordsLenght + 1) + j], vertices[(coordsLenght + 1) + (j % coordsLenght) + 1]]

            k += 2

        # Triangles faces cotés
        for i in range(1,coordsLenght + 1):
            triangles[k] = [vertices[i], vertices[(coordsLenght + 1) + (i % coordsLenght) + 1], vertices[(coordsLenght + 1) + i]]

            triangles[k + 1] = [vertices[i], vertices[(i % coordsLenght) + 1], vertices[(coordsLenght + 1) + (i % coordsLenght) + 1]]

            k += 2

        return triangles

    def parse_geom(self,path):
        # Realize the geometry conversion from geojson to GLTF
        # GLTF expect the geometry to only be triangles that contains 
        # the vertices position, i.e something in the form :  
        # [
        #   [np.array([0., 0., 0,]),
        #    np.array([0.5, 0.5, 0.5]),
        #    np.array([1.0 ,1.0 ,1.0])]
        #   [np.array([0.5, 0.5, 0,5]),
        #    np.array([1., 1., 1.]),
        #    np.array([-1.0 ,-1.0 ,-1.0])]
        # ]
        with open(path) as f:
            gjContent = json.load(f)

        for feature in gjContent['features']:
            coordinates = feature['geometry']['coordinates']
            coords = np.array(coordinates)
            coords = coords.flatten()

            coordsLenght = len(coords) // 3

            vertices = np.zeros(shape=(2 * (coordsLenght + 1), 3))
            height = 20
            if "HAUTEUR" in feature['properties']:
                if feature['properties']['HAUTEUR'] > 0:
                    height = feature['properties']['HAUTEUR']
            else:
                print("No propertie called HAUTEUR in feature")

            # Set bottom center vertice value
            vertices[0] = self.get_center(coords)
            # Set top center vertice value
            vertices[coordsLenght + 1] = [vertices[0][0], vertices[0][1] + height, vertices[0][2]]

            # For each coordinates, add a vertice at the coordinates and a vertice at the same coordinates with a Y-offset
            for i in range(0, coordsLenght):
                vertices[i + 1] = [coords[i * 3], coords[(i * 3) + 1], coords[(i * 3) + 2]]
                vertices[i + coordsLenght + 2] = [coords[i * 3], coords[(i * 3) + 1] + height, coords[(i * 3) + 2]]

        if(len(vertices)==0):
            return False

        triangles = self.create_triangles(vertices,coordsLenght)
        self.geom.triangles.append(triangles)

        self.set_box()

        return True

    def set_box(self):
        """
        Parameters
        ----------
        Returns
        -------
        """
        bbox = self.geom.getBbox()
        self.box = BoundingVolumeBox()
        self.box.set_from_mins_maxs(np.append(bbox[0],bbox[1]))
        
        # Set centroid from Bbox center
        self.centroid = np.array([(bbox[0][0] + bbox[1][0]) / 2.0,
                         (bbox[0][1] + bbox[1][1]) / 2.0,
                         (bbox[0][2] + bbox[0][2]) / 2.0])

    def get_geojson_id(self):
        return super().get_id()
    
    def set_geojson_id(self,id):
        return super().set_id(id)

class Geojsons(ObjectsToTile):
    """
        A decorated list of ObjectsToTile type objects.
    """
    def __init__(self,objs=None):
        super().__init__(objs)

    def translate_tileset(self,offset):
        """
        :param objects: an array containing geojsons 
        :param offset: an offset
        :return: 
        """
        # Translate the position of each geojson by an offset
        for geojson in self.objects:
            new_geom = []
            for triangle in geojson.get_geom_as_triangles():
                new_position = []
                for points in triangle:
                    # Must to do this this way to ensure that the new position 
                    # stays in float32, which is mandatory for writing the GLTF
                    new_position.append(np.array(points - offset, dtype=np.float32))
                new_geom.append(new_position)
            geojson.set_triangles(new_geom)
            geojson.set_box() 
    
    @staticmethod
    def retrieve_geojsons(path, objects=list()):
        """
        :param path: a path to a directory

        :return: a list of geojson. 
        """

        geojson_dir = listdir(path)

        for geojson_file in geojson_dir:
            if(os.path.isfile(os.path.join(path,geojson_file))):
                if(".geojson" in geojson_file or ".json" in geojson_file):
                    #Get id from its name
                    id = geojson_file.replace('json','')
                    geojson = Geojson(id)
                    #Create geometry as expected from GLTF from an geojson file
                    if(geojson.parse_geom(os.path.join(path,geojson_file))):
                        objects.append(geojson)
        
        return Geojsons(objects)    
