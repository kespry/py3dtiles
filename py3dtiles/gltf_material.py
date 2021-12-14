class GlTFMaterial():
    def __init__(self, metallicFactor=0, roughnessFactor=0, rgb=[1, 1, 1], alpha=1):
        self.metallicFactor = metallicFactor
        self.roughnessFactor = roughnessFactor
        self.rgba = rgb
        if len(rgb) < 4:
            self.alpha = alpha

    @property
    def metallicFactor(self):
        return self._metallicFactor

    @metallicFactor.setter
    def metallicFactor(self, value):
        if value > 1:
            value = 1
        if value < 0:
            value = 0
        self._metallicFactor = value

    @property
    def roughnessFactor(self):
        return self._roughnessFactor

    @roughnessFactor.setter
    def roughnessFactor(self, value):
        if value > 1:
            value = 1
        if value < 0:
            value = 0
        self._roughnessFactor = value

    @property
    def rgba(self):
        return self._rgba

    @rgba.setter
    def rgba(self, values):
        for value in values:
            if value > 1:
                value /= 255
            if value < 0:
                value = abs(value / 255)
        if len(values) < 3:
            for i in range(len(values), 3):
                values.append(0)
        elif len(values) > 4:
            values = values[:4]
        self._rgba = values

    @property
    def alpha(self):
        return self._rgba[3]

    @alpha.setter
    def alpha(self, value):
        if value > 1:
            value /= 255
        if value < 0:
            value = abs(value / 255)
        if len(self._rgba) < 4:
            self._rgba.append(value)
        else:
            self._rgba[3] = value

    @staticmethod
    def from_hexa(color_code='#FFFFFF'):
        hex = color_code.replace('#', '').replace('0x', '')
        length = max(len(hex), 8)
        rgb = [int(hex[i:i + 2], 16) / 255 for i in range(0, length, 2)]

        return GlTFMaterial(rgb=rgb)
