# -*- coding: utf-8 -*-

import unittest
import numpy as np
import json
from filecmp import cmp
# np.set_printoptions(formatter={'int':hex})

from py3dtiles import TileReader, B3dm, GlTF, TriangleSoup, GlTFMaterial, TilesetReader


class TestTileReader(unittest.TestCase):

    def test_read(self):
        tile = TileReader().read_file('tests/dragon_low.b3dm')

        self.assertEqual(tile.header.version, 1.0)
        self.assertEqual(tile.header.tile_byte_length, 47246)
        self.assertEqual(tile.header.ft_json_byte_length, 20)
        self.assertEqual(tile.header.ft_bin_byte_length, 0)
        self.assertEqual(tile.header.bt_json_byte_length, 0)
        self.assertEqual(tile.header.bt_bin_byte_length, 0)

        with open('tests/dragon_low_gltf_header.json', 'r') as f:
            gltf_header = json.loads(f.read())
        self.assertDictEqual(gltf_header, tile.body.glTF.header)


class TestTilesetReader(unittest.TestCase):

    def test_read(self):
        reader = TilesetReader()
        tileset = reader.read_tileset('tests/b3dm_tileset/')
        tiles = tileset.get_root_tile().get_children()
        tile_1_content = tiles[0].get_content()
        self.assertEqual(tile_1_content.header.version, 1.0)
        self.assertEqual(tile_1_content.header.tile_byte_length, 6180)
        self.assertEqual(tile_1_content.header.ft_json_byte_length, 4)
        self.assertEqual(tile_1_content.header.ft_bin_byte_length, 0)
        self.assertEqual(tile_1_content.header.bt_json_byte_length, 64)
        self.assertEqual(tile_1_content.header.bt_bin_byte_length, 0)
        self.assertEqual(len(tile_1_content.body.glTF.to_array()), 6084)

        tile_2_content = tiles[1].get_content()
        self.assertEqual(tile_2_content.header.version, 1.0)
        self.assertEqual(tile_2_content.header.tile_byte_length, 6796)
        self.assertEqual(tile_2_content.header.ft_json_byte_length, 4)
        self.assertEqual(tile_2_content.header.ft_bin_byte_length, 0)
        self.assertEqual(tile_2_content.header.bt_json_byte_length, 92)
        self.assertEqual(tile_2_content.header.bt_bin_byte_length, 0)
        self.assertEqual(len(tile_2_content.body.glTF.to_array()), 6672)

    def test_write(self):
        reader = TilesetReader()
        tileset = reader.read_tileset('tests/b3dm_tileset/')
        tileset.write_to_directory("junk/test_write_b3dm/")

        self.assertTrue(cmp('tests/b3dm_tileset/tileset.json', 'junk/test_write_b3dm/tileset.json'))
        self.assertTrue(cmp('tests/b3dm_tileset/tiles/0.b3dm', 'junk/test_write_b3dm/tiles/0.b3dm'))
        self.assertTrue(cmp('tests/b3dm_tileset/tiles/1.b3dm', 'junk/test_write_b3dm/tiles/1.b3dm'))


class TestTileBuilder(unittest.TestCase):

    def test_build(self):
        with open('tests/building.wkb', 'rb') as f:
            wkb = f.read()
        ts = TriangleSoup.from_wkb_multipolygon(wkb)
        positions = ts.getPositionArray()
        normals = ts.getNormalArray()
        box = [[-8.74748499994166, -7.35523200035095, -2.05385796777344],
               [8.8036420000717, 7.29930999968201, 2.05386103222656]]
        arrays = [{
            'position': positions,
            'normal': normals,
            'bbox': box,
            'matIndex': 0
        }]

        transform = np.array([
            [1, 0, 0, 1842015.125],
            [0, 1, 0, 5177109.25],
            [0, 0, 1, 247.87364196777344],
            [0, 0, 0, 1]], dtype=float)
        # translation : 1842015.125, 5177109.25, 247.87364196777344
        transform = transform.flatten('F')
        glTF = GlTF.from_binary_arrays(arrays, transform)
        t = B3dm.from_glTF(glTF)

        # get an array
        t.to_array()
        self.assertEqual(t.header.version, 1.0)
        self.assertEqual(t.header.tile_byte_length, 3016)
        self.assertEqual(t.header.ft_json_byte_length, 0)
        self.assertEqual(t.header.ft_bin_byte_length, 0)
        self.assertEqual(t.header.bt_json_byte_length, 0)
        self.assertEqual(t.header.bt_bin_byte_length, 0)

        # t.save_as("/tmp/py3dtiles_test_build_1.b3dm")


class TestTexturedTileBuilder(unittest.TestCase):

    def test_build(self):
        with open('tests/square.wkb', 'rb') as f:
            wkb = f.read()
        with open('tests/squareUV.wkb', 'rb') as f:
            wkbuv = f.read()
        ts = TriangleSoup.from_wkb_multipolygon(wkb, [wkbuv])
        positions = ts.getPositionArray()
        normals = ts.getNormalArray()
        uvs = ts.getDataArray(0)
        box = [[0, 0, 0],
               [10, 10, 0]]
        arrays = [{
            'position': positions,
            'normal': normals,
            'uv': uvs,
            'bbox': box,
            'matIndex': 0
        }]

        transform = np.array([
            [1, 0, 0, 0],
            [0, 1, 0, 0],
            [0, 0, 1, 0],
            [0, 0, 0, 1]], dtype=float)
        transform = transform.flatten('F')
        glTF = GlTF.from_binary_arrays(arrays, transform, materials=[GlTFMaterial(textureUri='squaretexture.jpg')])
        t = B3dm.from_glTF(glTF)

        # get an array
        t.to_array()
        self.assertEqual(t.header.version, 1.0)
        self.assertEqual(t.header.tile_byte_length, 1616)
        self.assertEqual(t.header.ft_json_byte_length, 0)
        self.assertEqual(t.header.ft_bin_byte_length, 0)
        self.assertEqual(t.header.bt_json_byte_length, 0)
        self.assertEqual(t.header.bt_bin_byte_length, 0)

        # t.save_as("/tmp/py3dtiles_test_build_1.b3dm")
