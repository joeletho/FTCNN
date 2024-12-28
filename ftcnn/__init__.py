import warnings

from osgeo import gdal

from .datasets import make_ndvi_difference_dataset
from .modeling import (
    AnnotatedLabel,
    BBox,
    ClassMap,
    ImageData,
    Serializable,
    XMLTree,
    XYInt,
    XYPair,
    YOLODatasetBase,
    YOLODatasetLoader,
)

__all__ = [
    "make_ndvi_difference_dataset",
    "YOLODatasetBase",
    "YOLODatasetLoader",
    "AnnotatedLabel",
    "ImageData",
    "BBox",
    "Serializable",
    "XMLTree",
    "XYInt",
    "XYPair",
    "ClassMap",
]

warnings.filterwarnings("ignore", "GeoSeries.notna", UserWarning)
gdal.UseExceptions()
