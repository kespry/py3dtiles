# -*- coding: utf-8 -*-

import sys
from py3dtiles import ThreeDTilesNotion, TileForReal

class TileSet(ThreeDTilesNotion):

    def __init__(self):
        super().__init__()
        self.header["asset"] = {"version": "1.0"}
        self.header["geometricError"] = None
        self.header["root"] = TileForReal()

    def set_geometric_error(self, error):
        self.header["geometricError"] = error

    def set_root_tile(self, root):
        if self.header["root"]:
            print("Warning: overwriting root tile.")
        self.header["root"] = root

    def prepare_for_json(self):
        """
        Convert to json string possibly mentioning used schemas
        """
        if not self.header["geometricError"]:
            self.set_geometric_error(1000000.0) # FIXME: chose a decent default
        if not self.header["root"]:
            print("A TileSet must have a root entry")
            sys.exit(1)