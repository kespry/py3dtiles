# -*- coding: utf-8 -*-

import numpy as np
import pyproj
import json
import sys
import os

from .bounding_volume_box import BoundingVolumeBox
from .pnts import Pnts
from .b3dm import B3dm
from .tile import Tile
from .tileset import TileSet


def convert_to_ecef(x, y, z, epsg_input):
    inp = pyproj.Proj(init='epsg:{0}'.format(epsg_input))
    outp = pyproj.Proj(init='epsg:4978')  # ECEF
    return pyproj.transform(inp, outp, x, y, z)


class TileReader(object):

    def read_file(self, filename):
        with open(filename, 'rb') as f:
            data = f.read()
            arr = np.frombuffer(data, dtype=np.uint8)
            return self.read_array(arr)
        return None

    def read_array(self, array):
        magic = ''.join([c.decode('UTF-8') for c in array[0:4].view('c')])
        if magic == 'pnts':
            return Pnts.from_array(array)
        if magic == 'b3dm':
            return B3dm.from_array(array)
        return None


class TilesetReader(object):

    def __init__(self):
        self.tile_reader = TileReader()

    def read_tileset(self, path):
        """
        param: path: Path to a directory containing the tileset.json
        """
        with open(os.path.join(path, 'tileset.json')) as f:
            json_tileset = json.load(f)
        root = json_tileset['root']
        tileset = TileSet()

        for child in root['children']:
            self.read_tile(child, tileset, path, 0)

        tileset.get_root_tile().set_bounding_volume(BoundingVolumeBox())
        return tileset

    def read_tile(self, json_tile, parent, path, depth=0):
        tile = Tile()
        tile.set_geometric_error(json_tile['geometricError'])
        uri = os.path.join(path, json_tile['content']['uri'])
        tile.set_content(self.tile_reader.read_file(uri))
        tile.set_transform(json_tile['transform'])
        tile.set_refine_mode(json_tile['refine'])

        if 'box' in json_tile['boundingVolume']:
            bounding_volume = BoundingVolumeBox()
            bounding_volume.set_from_list(json_tile['boundingVolume']['box'])
        else:
            print('Sphere and region bounding volumes aren\'t supported')
            sys.exit(1)
        tile.set_bounding_volume(bounding_volume)

        if depth == 0:
            parent.add_tile(tile)
        else:
            parent.add_child(tile)

        if 'children' in json_tile:
            for child in json_tile['children']:
                self.read_tile(child, tile, path, depth + 1)
