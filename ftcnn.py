import io
import os
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from ctypes import ArgumentError
from pathlib import Path
from time import sleep, time
from types import FunctionType

import geopandas as gpd
import numpy as np
import pandas as pd
import rasterio
import rioxarray as rxr
import shapely
import skimage.io as skio
from osgeo import gdal, gdal_array
from pandas.compat import sys
from PIL import Image
from rasterio.windows import Window
from shapely import normalize, wkt
from shapely.geometry import Polygon
from skimage import img_as_float
from tqdm.auto import tqdm, trange

from .utils import (Lock, clear_directory, collect_files_with_suffix,
                    get_cpu_count, pathify)

TQDM_INTERVAL = 1 / 100

FTCNN_TMP_DIR = Path("/tmp", "ftcnn")

warnings.filterwarnings("ignore", "GeoSeries.notna", UserWarning)
gdal.UseExceptions()


_writelock = Lock()


def __request_writelock__():
    global _writelock
    while _writelock.is_locked():
        sleep(1 / 50)
    _writelock.lock()
    return _writelock


def __free_writelock__(lock):
    lock.unlock()
    return None


def open_geo_ds(path):
    if not isinstance(path, str):
        path = str(path)
    return gdal.Open(path)


def save_as_shp(gdf: gpd.GeoDataFrame, path, exist_ok=False):
    path = pathify(path)
    if not exist_ok and path.exists():
        raise FileExistsError(path)
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)

    gdf.to_file(path, driver="ESRI shapefile")


def save_as_gpkg(gdf: gpd.GeoDataFrame, path, exist_ok=False):
    path = pathify(path)
    if not exist_ok and path.exists():
        raise FileExistsError(path)
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)

    gdf.to_file(path, driver="GPKG")


def save_as_csv(df: pd.DataFrame | gpd.GeoDataFrame, path, exist_ok=False):
    path = pathify(path)
    if not exist_ok and path.exists():
        raise FileExistsError(path)
    if not path.parent.exists():
        path.parent.mkdir(parents=True, exist_ok=True)

    df.to_csv(path, index=False)


def load_shapefile(path) -> gpd.GeoDataFrame:
    if Path(path).suffix == ".csv":
        df = gpd.read_file(path)
        df["geometry"] = df["geometry"].apply(wkt.loads)
        gpkg_df = gpd.GeoDataFrame(df)
    else:
        gpkg_df = gpd.read_file(path)
    return gpkg_df


def open_tif(path, *, masked=True):
    return rxr.open_rasterio(path, masked=masked).squeeze()


def encode_default_classes(row):
    geom = row.get("geometry")
    return (
        (0, "Treatment")
        if geom is not None and not geom.is_empty and geom.area > 1
        else (-1, "Background")
    )


def parse_filename(series: pd.Series):
    subregion = str(series["Subregion"])
    startyear = str(series["StartYear"])
    endyear = str(series["EndYear"])

    years_part = "to".join([startyear, endyear])
    end_part = "NDVI_Difference.tif"

    filename = subregion
    last = filename[-1]
    if last.isdigit():
        filename += "_"
    elif last == "E":
        filename = "_".join([filename[:-1], "Expanded", ""])
    start_part = filename + years_part
    return "_".join([start_part, end_part])


def shapefile_to_csv(src_path, dest_path):
    pbar = trange(2, desc="Reading file", leave=False)
    data = gpd.read_file(src_path)
    pbar.update()
    pbar.set_description("Saving to output file")
    data.to_csv(dest_path, index=False)
    pbar.update()
    pbar.close()
    print("Complete")


def flatten_geom(gdf_src, geometry_column="geometry", group_by=None, leave=True):
    geometry = []
    rows = []

    df_group = gdf_src.groupby(group_by, sort=False)

    total_updates = len(df_group) + 1
    updates = 0
    pbar = trange(total_updates, desc="Flattening geometry", leave=leave)
    start = time()

    for _, group in df_group:
        polygon = shapely.unary_union(group.geometry)
        if isinstance(polygon, shapely.MultiPolygon):
            for i, poly in enumerate(polygon.geoms):
                poly = normalize(poly)
                row = group.iloc[i].drop(geometry_column).to_dict()
                for bbox in get_geom_bboxes(polygon):
                    row["bbox"] = bbox
                    rows.append(row)
                    geometry.append(polygon)
        else:
            polygon = normalize(polygon)
            row = group.iloc[0].drop(geometry_column).to_dict()
            for bbox in get_geom_bboxes(polygon):
                row["bbox"] = bbox
                rows.append(row)
                geometry.append(polygon)
        if time() - start >= TQDM_INTERVAL:
            pbar.update()
            updates += 1
            start = time()

    gdf = gpd.GeoDataFrame(rows, geometry=geometry, crs=gdf_src.crs)
    if updates < total_updates:
        pbar.update(total_updates - updates)
    pbar.set_description("Flattening geometry. Complete")
    pbar.close()

    return gdf


def map_metadata(
    df_src,
    start_year_col,
    end_year_col,
    img_dir,
    parse_filename=parse_filename,
    leave=True,
) -> gpd.GeoDataFrame:
    img_dir = Path(img_dir).resolve()
    columns = {
        "start_year": [],
        "end_year": [],
        "filename": [],
        "path": [],
        "width": [],
        "height": [],
        "bbox": [],
    }
    rows = []
    geometry = []

    total_updates = len(df_src) + 1
    updates = 0
    start = time()
    pbar = trange(total_updates, desc="Collecting image data", leave=leave)
    for _, row in df_src.iterrows():
        filename = parse_filename(row)
        path = img_dir / filename

        if path.exists():
            for r in rows:
                if path == r["path"]:
                    continue
            suffix = path.suffix
            open_fn = open_tif if suffix in [".tiff", ".tif"] else Image.open

            with open_fn(path) as img:
                width = str(img.shape[0])
                height = str(img.shape[1])
                rows.append(
                    {
                        "start_year": int(row[start_year_col]),
                        "end_year": int(row[end_year_col]),
                        "filename": filename,
                        "path": path,
                        "width": width,
                        "height": height,
                        "bbox": row["bbox"],
                    }
                )
                geometry.append(row["geometry"])
        if time() - start >= TQDM_INTERVAL:
            pbar.update()
            updates += 1
            start = time()

    pbar.set_description("Populating dataframe")

    df_dest = gpd.GeoDataFrame(rows, columns=columns, geometry=geometry, crs=df_src.crs)

    if updates < total_updates:
        pbar.update(total_updates - updates)
    pbar.set_description("Collecting image data. Complete")
    pbar.close()

    return df_dest


def preprocess_shapefile(
    shpfile,
    start_year_col,
    end_year_col,
    img_dir,
    leave=True,
) -> gpd.GeoDataFrame:
    gdf = load_shapefile(shpfile)
    crs = gdf.crs

    gdf = flatten_geom(gdf, group_by=[start_year_col, end_year_col], leave=leave)
    gdf = gdf.drop_duplicates()
    gdf = map_metadata(
        gdf,
        start_year_col=start_year_col,
        end_year_col=end_year_col,
        img_dir=img_dir,
        leave=leave,
    )
    gdf = gdf.drop_duplicates()
    return gpd.GeoDataFrame(gdf, crs=crs)


def get_geom_points(geom):
    match (geom.geom_type):
        case "Polygon":
            points = [point for point in normalize(geom.exterior.coords)]
        case "MultiPolygon":
            points = [
                [point for point in normalize(polygon.exterior.coords)]
                for polygon in geom.geoms
            ]
        case _:
            raise ValueError("Unknown geometry type")
    return points


def get_geom_bboxes(geom):
    boxes = []
    match (geom.geom_type):
        case "Polygon":
            boxes.append(normalize(shapely.box(*geom.bounds)))
        case "MultiPolygon":
            for geom in geom.geoms:
                boxes.append(normalize(shapely.box(*geom.bounds)))
        case _:
            print("Unknown geometry type")
    return boxes


def stringify_points(points):
    return " ".join([f"{point[0]} {point[1]}" for point in points])


def stringify_bbox(bbox):
    return f"{' '.join([str(x) for x in bbox])}"


def parse_points_list_str(s):
    points = []
    i = 0
    while i < len(s):
        if s[i] == "(":
            i += 1
            stop = s.index(")", i)
            point = s[i:stop].split(",")
            points.append((float(point[0]), float(point[1])))
            i = stop
        else:
            i += 1
    return points


def get_geom(df, *, geom_key="geometry", parse_key: FunctionType | None = None):
    geoms = []
    for _, row in df.iterrows():
        if parse_key is not None:
            geom = parse_key(row)
            if geom is not None:
                geoms.append(geom)
        else:
            geoms.append(row[geom_key])
    return geoms


def get_geom_polygons(geom, *, flatten=False):
    polygons = []

    match (geom.geom_type):
        case "Polygon":
            polygons.append(Polygon(geom))
        case "MultiPolygon":
            if not flatten:
                polygons.extend([Polygon(g) for g in list(geom.geoms)])
            else:
                flattened = []
                for polygon in [Polygon(p) for p in list(geom.geoms)]:
                    if len(flattened) == 0:
                        flattened.append(polygon)
                        continue
                    found = False
                    for i, flat in enumerate(flattened):
                        union = shapely.coverage_union(flat, polygon)
                        for poly in union.geoms:
                            # There is no union
                            if poly.equals(flat) or poly.equals(polygon):
                                continue
                            # Replace this polygon with the union
                            flattened[i] = poly
                            found = True
                            break
                        if found:
                            break
                    if not found:
                        flattened.append(polygon)
                polygons.extend(flattened)

        case _:
            raise ValueError("Unknown geometry type")
    return polygons


def normalize_shapefile(path):
    df_in = load_shapefile(path)
    columns = {key: [] for key in [*df_in.columns, "bbox"]}
    df_out = gpd.GeoDataFrame(columns=columns)
    df_out.drop("geometry", axis=1)

    pbar = trange(len(df_in), desc="Normalizing", leave=False)
    for _, row in df_in.iterrows():
        row_out = dict(row)
        geom = row_out["geometry"]
        polygons = get_geom_polygons(geom)
        boxes = get_geom_bboxes(geom)
        for i, polygon in enumerate(polygons):
            row_out["bbox"] = [boxes[i]]
            row_out["geometry"] = [polygon]
            df_row = gpd.GeoDataFrame.from_dict(row_out)
            df_out = pd.concat([df_out, df_row])

        pbar.update()
    pbar.set_description("Complete")
    pbar.close()
    return df_out


def normalize_shapefile_with_metadata(shpfile, dir, filename_key=parse_filename):
    dir = Path(dir).resolve()
    df_out = normalize_shapefile(shpfile)
    columns = {
        "filename": [],
        "path": [],
        "width": [],
        "height": [],
    }
    total_updates = len(df_out) + 1
    updates = 0
    start = time()
    pbar = trange(total_updates, desc="Collecting image data")
    for _, row in df_out.iterrows():
        filename = filename_key(row)
        path = dir / filename
        try:
            tif = open_tif(path)
        except Exception:
            # Populate the columns with no data and continue
            columns["filename"].append("None")
            columns["path"].append("None")
            columns["width"].append("None")
            columns["height"].append("None")
            pbar.update()
            continue

        width = str(tif.shape[0])
        height = str(tif.shape[1])
        columns["filename"].append(filename)
        columns["path"].append(path)
        columns["width"].append(width)
        columns["height"].append(height)
        if time() - start >= TQDM_INTERVAL:
            pbar.update()
            updates += 1
            start = time()

    pbar.set_description("Populating dataframe")
    df_out = df_out.assign(**columns)

    if updates < total_updates:
        pbar.update(total_updates - updates)
    pbar.set_description("Complete")
    pbar.close()

    return df_out


def encode_classes(df: pd.DataFrame, encoder=encode_default_classes):
    columns = {"class_id": [], "class_name": []}
    total_updates = len(df) + 1
    updates = 0
    start = time()
    pbar = trange(len(df) + 1, desc="Encoding class data")
    for _, row in df.iterrows():
        id, name = encoder(row)
        columns["class_id"].append(id)
        columns["class_name"].append(name)
        if time() - start >= TQDM_INTERVAL:
            pbar.update()
            updates += 1
            start = time()
    df_encoded = df.copy()
    df_encoded.insert(0, "class_id", columns["class_id"])
    df_encoded.insert(1, "class_name", columns["class_name"])

    if updates < total_updates:
        pbar.update(total_updates - updates)
    pbar.set_description("Complete")
    pbar.close()
    return df_encoded


def write_chip(data, *, transform, meta, output_path=None):
    meta.update(
        {
            "height": data.shape[0],
            "width": data.shape[1],
            "transform": transform,
        }
    )
    res = None
    lock = None
    try:
        lock = __request_writelock__()
        if output_path is None:
            # Create an in-memory Image
            chip = io.BytesIO()
            with rasterio.MemoryFile(chip) as mem:
                with mem.open(**meta) as dest:
                    dest.write(data, 1)
                with mem.open() as src:
                    arr = src.read(1)
                    res = arr.reshape(data.shape)
        else:
            with rasterio.open(output_path, "w", **meta) as dest:
                dest.write(data, 1)  # 1 --> single-band
        lock = __free_writelock__(lock)
    except Exception as e:
        if lock is not None:
            lock = __free_writelock__(lock)
        raise (e)
    return res


def create_chips_from_geotiff(
    geotiff_path, chip_size, crs, output_dir=None, exist_ok=False, leave=False
):
    geotiff_path = Path(geotiff_path)
    width = chip_size[0]
    height = chip_size[1]
    chips = []

    with rasterio.open(geotiff_path, crs=crs) as src:
        bounds = src.bounds
        rmin, cmin = src.index(bounds.left, bounds.top)
        rmax, cmax = src.index(bounds.right, bounds.bottom)
        rmin, rmax = min(rmin, rmax), max(rmin, rmax)
        cmin, cmax = min(cmin, cmax), max(cmin, cmax)

        if width is None or height is None:
            width = cmax - cmin
            height = rmax - rmin

        if width <= 0 or height <= 0:
            return []

        total_updates = (rmax // height) * (cmax // width)
        updates = 0
        start = time()
        pbar = trange(
            total_updates, desc=f"Processing {geotiff_path.name}", leave=leave
        )

        for row in range(rmin, rmax, height):
            for col in range(cmin, cmax, width):
                chip_output_path = None
                if output_dir is not None:
                    output_dir = Path(output_dir)
                    chip_output_path = (
                        output_dir / f"{geotiff_path.stem}_chip_{row}_{col}.tif"
                    )
                    if chip_output_path.exists() and not exist_ok:
                        raise FileExistsError(
                            f"File '{chip_output_path}' aleady exists"
                        )
                    output_dir.mkdir(parents=True, exist_ok=True)

                rem_height = rmax - row - 1
                rem_width = cmax - col - 1

                chip_window = Window(
                    col_off=col,
                    row_off=row,
                    width=min(width, rem_width),
                    height=min(height, rem_height),
                )
                chip_data = src.read(1, window=chip_window)
                chip_data[chip_data == src.nodata] = np.NaN

                # does the image have relevant data?
                if (
                    chip_data.shape[0] == 0
                    or chip_data.shape[1] == 0
                    or chip_data.max() == 0
                    or chip_data.max() == src.nodata
                ) or (
                    # Set a threshold incase one or more of the dims are very small
                    min(chip_data.shape[0], chip_data.shape[1])
                    / max(chip_data.shape[0], chip_data.shape[1])
                    < 0.1
                ):
                    continue

                chip_transform = src.window_transform(chip_window)
                chips.append(
                    (
                        write_chip(
                            chip_data,
                            transform=chip_transform,
                            meta=src.meta.copy(),
                            output_path=chip_output_path,
                        ),
                        src.xy(row, col),
                    )
                )
                if time() - start >= TQDM_INTERVAL:
                    pbar.update()
                    updates += 1
                    start = time()
    if leave:
        pbar.set_description(f"{geotiff_path.name} processed.")
    if updates < total_updates:
        pbar.update(total_updates - updates)
    pbar.close()
    return chips


def collect_filepaths(df: pd.DataFrame, column_name):
    return list(df.loc[:, column_name].values())


def preprocess_geotiff_dataset(
    gdf: gpd.GeoDataFrame,
    output_dir,
    years=None,
    img_path_col="path",
    start_year_col="StartYear",
    end_year_col="EndYear",
    clean_dest=False,
):
    if years is None:

        def all_images(df):
            images = df.loc[:, img_path_col].unique().tolist()
            return images

        get_filepaths = all_images
    else:

        def match_years(df):
            return (
                df.loc[
                    (df[start_year_col] == years[0]) & (df[end_year_col] == years[1]),
                    img_path_col,
                ]
                .unique()
                .tolist()
            )

        get_filepaths = match_years

    imgs = pathify(get_filepaths(gdf))
    if not isinstance(imgs, list):
        imgs = [imgs]

    if len(imgs) == 0:
        raise Exception("Could not find images")

    output_dir = output_dir if isinstance(output_dir, Path) else Path(output_dir)

    if clean_dest:
        clear_directory(output_dir)

    return imgs


def preprocess_ndvi_difference_geotiffs(
    gdf: gpd.GeoDataFrame,
    output_dir,
    *,
    years=None,
    img_path_col="path",
    start_year_col="start_year",
    end_year_col="end_year",
    geom_col="geometry",
    chip_size=None,
    exist_ok=False,
    clean_dest=False,
    ignore_empty_geom=True,
    leave=True,
    num_workers=None,
):
    imgs = preprocess_geotiff_dataset(
        gdf,
        output_dir,
        years,
        img_path_col,
        start_year_col,
        end_year_col,
        clean_dest,
    )

    chip_size = chip_size if chip_size is not None else (None, None)
    if not isinstance(chip_size, tuple):
        chip_size = (chip_size, chip_size)

    pbar = trange(
        len(imgs) + 2,
        desc=f"Creating GeoTIFF chips of size {f'({chip_size[0]},{chip_size[1]})' if chip_size[0] is not None else 'Default'}",
        leave=leave,
    )

    num_workers = num_workers if num_workers else get_cpu_count()

    # Make the chips for each GeoTIFF
    with ThreadPoolExecutor(max_workers=num_workers) as executor:
        futures = [
            executor.submit(
                create_chips_from_geotiff,
                path,
                crs=gdf.crs,
                chip_size=chip_size,
                output_dir=output_dir / path.stem,
                exist_ok=exist_ok,
            )
            for path in imgs
        ]
        for _ in as_completed(futures):
            pbar.update()

    # Group rows by the years in which they span. We want to ensure we are only
    # applying geometry to rows which share the same start and end years.
    year_pairs = gdf[[start_year_col, end_year_col]].drop_duplicates()
    year_pairs = year_pairs.sort_values(by=[start_year_col, end_year_col])
    start_years = [int(year) for year in year_pairs[start_year_col].tolist()]
    end_years = [int(year) for year in year_pairs[end_year_col].tolist()]

    mapped_gdfs = []
    for start_year, end_year in zip(start_years, end_years):
        # Get the rows which match the start and end years
        target_years = gdf[
            (gdf[start_year_col] == start_year) & (gdf[end_year_col] == end_year)
        ]
        gdf_mapped = map_geometry_to_geotiffs(
            target_years,
            output_dir,
        )
        # Insert the year columns into the mapped geodf
        gdf_mapped.insert(0, start_year_col, start_year)
        gdf_mapped.insert(1, end_year_col, end_year)

        mapped_gdfs.append(gdf_mapped)

    gdf = gpd.GeoDataFrame(pd.concat(mapped_gdfs, ignore_index=True), crs=gdf.crs)
    gdf.set_geometry("geometry", inplace=True)

    pbar.update()

    chip_paths = [
        str(path)
        for path in collect_files_with_suffix(".tif", output_dir, recurse=True)
    ]
    unused_chips = []

    if ignore_empty_geom:
        # Remove any chips that do not map to an image in the dataframe
        unused_chips = gdf.loc[gdf[geom_col].is_empty, img_path_col].tolist()
        gdf = gdf[~gdf[geom_col].is_empty].reset_index(drop=True)

        for path in tqdm(unused_chips, desc="Cleaning up", leave=False):
            path = Path(path)
            parent = path.parent
            parent_parent = parent.parent
            if path.exists():
                os.remove(path)
            if parent.exists() and len(os.listdir(parent)) == 0:
                os.rmdir(parent)
                if parent_parent.exists() and len(os.listdir(parent_parent)) == 0:
                    os.rmdir(parent_parent)

    pbar.update()
    nfiles = max(0, len(chip_paths) - len(unused_chips))
    pbar.set_description(
        "Processed {0} images and saved to {1}".format(nfiles, output_dir)
    )
    pbar.close()

    gdf = gdf.drop_duplicates().sort_values(by=[start_year_col, end_year_col])
    return gdf.reset_index(drop=True)


def make_ndvi_dataset(
    shp_file,
    ndvi_dir,
    output_dir,
    *,
    years=None,
    start_year_col="start_year",
    end_year_col="end_year",
    geom_col="geometry",
    chip_size=None,
    clean_dest=False,
    xy_to_index=True,
    exist_ok=False,
    save_csv=False,
    save_shp=False,
    save_gpkg=False,
    ignore_empty_geom=True,
    tif_to_png=True,
    pbar_leave=True,
    num_workers=None,
):
    if shp_file is None:
        raise ArgumentError("Missing path to shape file")
    if ndvi_dir is None:
        raise ArgumentError("Missing path to NDVI images")
    if output_dir is None:
        raise ArgumentError("Missing path to output directory")

    shp_file, ndvi_dir, output_dir = pathify(shp_file, ndvi_dir, output_dir)
    meta_dir = output_dir / "meta"
    csv_dir = meta_dir / "csv" / shp_file.stem
    shp_dir = meta_dir / "shp" / shp_file.stem

    if output_dir.exists() and clean_dest:
        clear_directory(output_dir)
    elif not output_dir.exists():
        output_dir.mkdir(parents=True)

    if save_csv:
        csv_dir.mkdir(parents=True, exist_ok=exist_ok)
    if save_shp or save_gpkg:
        shp_dir.mkdir(parents=True, exist_ok=exist_ok)

    chips_dir = output_dir / "images" / "chips"
    chips_dir.mkdir(parents=True, exist_ok=exist_ok)

    n_calls = 4
    n_calls += 1 if xy_to_index else 0
    n_calls += 1 if tif_to_png else 0

    pbar = trange(
        n_calls,
        desc="Creating NDVI dataset - Preprocessing shapefile",
        leave=pbar_leave,
    )

    gdf = preprocess_shapefile(
        shp_file,
        start_year_col=start_year_col,
        end_year_col=end_year_col,
        img_dir=ndvi_dir,
        leave=False,
    )
    pbar.update()

    start_year_col = "start_year"
    end_year_col = "end_year"

    output_fname = shp_file.stem
    if years is not None:
        output_fname += f"_{years[0]}to{years[1]}"
    output_fname = Path(output_fname)

    if save_csv:
        save_as_csv(gdf, csv_dir / output_fname.with_suffix(".csv"))
    if save_shp:
        save_as_shp(
            gdf,
            shp_dir / output_fname.with_suffix(".shp"),
        )
    if save_gpkg:
        save_as_gpkg(
            gdf,
            shp_dir / output_fname.with_suffix(".gpkg"),
        )

    pbar.update()
    pbar.set_description("Creating NDVI dataset - Preprocessing GeoTIFFs")

    gdf = preprocess_ndvi_difference_geotiffs(
        gdf,
        chips_dir,
        start_year_col=start_year_col,
        end_year_col=end_year_col,
        geom_col=geom_col,
        years=years,
        chip_size=chip_size,
        clean_dest=clean_dest,
        exist_ok=exist_ok,
        ignore_empty_geom=ignore_empty_geom,
        leave=False,
        num_workers=num_workers,
    )
    pbar.update()

    if save_csv or save_shp:
        output_fname = Path(f"{output_fname}_chips_xy")
        if save_csv:
            save_as_csv(gdf, csv_dir / output_fname.with_suffix(".csv"))
        if save_shp:
            save_as_shp(
                gdf,
                shp_dir / output_fname.with_suffix(".shp"),
            )
        if save_gpkg:
            save_as_gpkg(
                gdf,
                shp_dir / output_fname.with_suffix(".gpkg"),
            )

    if xy_to_index:
        pbar.update()
        pbar.set_description("Creating NDVI dataset - Translating xy coords to index")

        gdf = translate_xy_coords_to_index(gdf, leave=False)
        if save_csv or save_shp:
            output_fname = str(output_fname).replace("_xy", "_indexed")
            if not output_fname.endswith("_indexed"):
                output_fname += "_indexed"
            output_fname = Path(output_fname)
            if save_csv:
                save_as_csv(gdf, csv_dir / output_fname.with_suffix(".csv"))
            if save_shp:
                save_as_shp(
                    gdf,
                    shp_dir / output_fname.with_suffix(".shp"),
                )
            if save_gpkg:
                save_as_gpkg(
                    gdf,
                    shp_dir / output_fname.with_suffix(".gpkg"),
                )
    if tif_to_png:
        pbar.update()
        pbar.set_description("Creating NDVI dataset - Converting GeoTIFFs to PNGs")

        tif_png_file_map = process_geotiff_to_png_conversion(
            chips_dir, output_dir / "images" / "png-chips", leave=False
        )
        spbar = trange(len(gdf), desc="Mapping filepaths", leave=False)
        for i, row in gdf.iterrows():
            tif_file = str(row["filename"])
            paths = tif_png_file_map.get(Path(tif_file).stem)
            if paths is not None:
                gdf.loc[i, "filename"] = paths["png"].name
                gdf.loc[i, "path"] = paths["png"]
            spbar.update()
        spbar.close()
        if save_csv or save_shp:
            output_fname = Path(f"{output_fname}_as_png")
            if save_csv:
                save_as_csv(gdf, csv_dir / output_fname.with_suffix(".csv"))
            if save_shp:
                save_as_shp(
                    gdf,
                    shp_dir / output_fname.with_suffix(".shp"),
                )
            if save_gpkg:
                save_as_gpkg(
                    gdf,
                    shp_dir / output_fname.with_suffix(".gpkg"),
                )
    pbar.update()
    pbar.set_description("Creating NDVI dataset - Complete")
    pbar.close()

    return gdf, (meta_dir, chips_dir, output_fname)


"""
    This might be the issue which causes the label issues where geometry 
    does not align with the actual geom,also causing the labeled geom to 
    appear "clipped" overlayed on the image
"""


def create_chip_polygon(src, chip_window):
    chip_transform = src.window_transform(chip_window)
    width, height = chip_window.width, chip_window.height

    chip_polygon = Polygon(
        [
            chip_transform * (0, 0),  # Top-left
            chip_transform * (width, 0),  # Top-right
            chip_transform * (width, height),  # Bottom-right
            chip_transform * (0, height),  # Bottom-left
            chip_transform * (0, 0),  # Close the polygon (back to top-left)
        ]
    )
    return chip_polygon


def map_geometry_to_geotiffs(
    gdf: gpd.GeoDataFrame, img_dir, recurse=True
) -> gpd.GeoDataFrame:
    img_dir = Path(img_dir).resolve()
    columns = [
        "filename",
        "path",
        "width",
        "height",
    ]
    rows = []
    geometry = []

    orig_stems = [
        os.path.splitext(filename)[0] for filename in gdf["filename"].unique().tolist()
    ]

    def compare_stem(stem, names):
        for name in names:
            if stem[: len(name)] in name:
                return True
        return False

    image_paths = [
        path
        for path in collect_files_with_suffix(".tif", img_dir, recurse=recurse)
        if compare_stem(path.stem, orig_stems)
    ]

    for path in tqdm(
        image_paths,
        desc="Mapping geometry to GeoTIFFs",
        leave=False,
    ):
        with rasterio.open(path) as src:
            chip_window = Window(
                col_off=0, row_off=0, width=src.width, height=src.height
            )
            chip_polygon = create_chip_polygon(src, chip_window)
            intersecting_polygons = gdf.loc[gdf.intersects(chip_polygon)]

            row = {
                "filename": path.name,
                "path": str(path),
                "width": src.width,
                "height": src.height,
            }

            if not intersecting_polygons.empty:
                for _, polygon_row in intersecting_polygons.iterrows():
                    geometry.append(polygon_row["geometry"].intersection(chip_polygon))
                    rows.append(row)
            else:
                geometry.append(Polygon())
                rows.append(row)

    return gpd.GeoDataFrame(
        gpd.GeoDataFrame(rows, columns=columns, geometry=geometry, crs=gdf.crs)
        .explode()
        .drop_duplicates()
    )


def translate_xy_coords_to_index(gdf: gpd.GeoDataFrame, *, leave=True):
    gdf = gdf.copy()
    pbar = trange(len(gdf), desc="Translating geometry", leave=leave)
    for i, row in gdf.iterrows():
        if Path(str(row["path"])).exists() and isinstance(row["geometry"], Polygon):
            gdf.loc[i, "geometry"] = Polygon(
                geotiff_convert_geometry_to_pixels(row["path"], row["geometry"])
            )
        pbar.update()
    if leave:
        pbar.set_description("Complete")
    pbar.close()
    return gdf


def parse_polygon_str(polygon_str: str):
    size = len(polygon_str)
    start = 0
    end = size - 1
    while (
        start < end
        and start < size
        and end >= 0
        and not (polygon_str[start].isdigit() and polygon_str[end].isdigit())
    ):
        if not polygon_str[start].isdigit():
            start += 1
        if not polygon_str[end].isdigit():
            end -= 1

    if start < size and end >= 0:
        polygon_str = polygon_str[start : end + 1]
    parsed = []
    points = polygon_str.split(", ")
    for point in points:
        point = point.replace(" 0", "").replace("(", "").replace(")", "")
        x, y = point.split()
        parsed.append((float(x), float(y)))

    return parsed


def geotiff_convert_geometry_to_pixels(tiff_path, geometry):
    def __append_point_pixels(points, pixels, src):
        for point in points:
            pixels.append(
                src.index(point[0], point[1])[::-1],
            )

    if not isinstance(geometry, Polygon):
        if isinstance(geometry, str):
            geometry = parse_polygon_str(geometry)
        else:
            raise ValueError(f"Unknown type '{type(geometry)}'")
        geometry = Polygon(geometry)

    polygon = normalize(geometry.simplify(0.002, preserve_topology=True))

    pixels = []
    with rasterio.open(tiff_path) as src:
        geom_points = list(polygon.exterior.coords)
        __append_point_pixels(geom_points, pixels, src)
    return clip_points(pixels, (src.width, src.height))


def geotiff_convert_pixels_to_geometry(tiff_path, pixels):
    def __append_point_coords(points, coords, src):
        for point in points:
            coords.append(
                src.xy(point[1], point[0]),
            )

    if not isinstance(pixels, Polygon):
        if isinstance(pixels, list):
            if not isinstance(pixels[0], tuple):
                pixels = [(pixels[i], pixels[i + 1]) for i in range(len(pixels) - 1)]
        elif isinstance(pixels, str):
            pixels = parse_polygon_str(pixels)
        else:
            raise ValueError(f"Unknown type '{type(pixels)}'")
        pixels = normalize(Polygon(pixels))

    polygon = pixels.simplify(0.002, preserve_topology=True)

    coords = []
    with rasterio.open(tiff_path) as src:
        pixel_points = list(normalize(polygon.exterior.coords))
        __append_point_coords(pixel_points, coords, src)
    return Polygon(coords)


def clip_points(points, shape):
    width = shape[0]
    height = shape[1]
    clipped = []
    for x, y in points:
        x = max(0, min(x, width - 1))
        y = max(0, min(y, height - 1))
        clipped.append((x, y))
    return clipped


def tiff_to_png(tiff, out_path=None):
    if isinstance(tiff, str) or isinstance(tiff, Path):
        src_ds = open_geo_ds(tiff)
    else:
        src_ds = gdal_array.OpenArray(tiff)

    lock = None
    png_img = None
    try:
        src_width = src_ds.RasterXSize
        src_height = src_ds.RasterYSize

        # Create a new in-memory dataset with 3 bands (for RGB)
        rgb_ds = gdal.GetDriverByName("MEM").Create(
            "", src_width, src_height, 3, gdal.GDT_Float64
        )

        # Read the data from the source band
        raster_band = src_ds.GetRasterBand(1)

        # Store the nodata value
        nodata_value = raster_band.GetNoDataValue()

        # Read band data
        data = raster_band.ReadAsArray()

        # Close the source
        src_ds = None

        # Replace NoData values with NaN
        if nodata_value is not None:
            mask = data != nodata_value
            data = np.where(mask, data, np.nan)

        # Interpolate valid values, excluding NaN
        valid_data = data[~np.isnan(data)]
        if valid_data.size > 0:
            data = np.interp(data, (valid_data.min(), valid_data.max()), (0, 1))
        else:
            # Populate with 0 if no valid data exists
            data = np.zeros_like(data)

        # Write the normalized data to each band of the new dataset
        for band in range(1, 4):
            rgb_ds.GetRasterBand(band).WriteArray(data)
        rgb_ds.FlushCache()

        # Read data as an RGB array (shape: 3 x height x width)
        png_data = rgb_ds.ReadAsArray()
        # Free resources
        rgb_ds = None

        # Left-rotate the array (shape: height x width x 3)
        png_data = np.moveaxis(png_data, 0, -1)

        # Prepare the new image and save (if required)
        png_img = (img_as_float(png_data) * 255).astype(np.uint8)
        if out_path is not None:
            lock = __request_writelock__()
            skio.imsave(out_path, png_img, check_contrast=False)
            lock = __free_writelock__(lock)
    except Exception as e:
        if lock is not None:
            lock = __free_writelock__(lock)
        print(e, file=sys.stderr)

    return png_img


def process_geotiff_to_png_conversion(
    src_dir, dest_dir, *, recurse=True, preserve_dir=True, clear_dir=True, leave=True
):
    file_map = {}

    src_dir = Path(src_dir).absolute()
    dest_dir = Path(dest_dir).absolute()

    src_paths = collect_files_with_suffix(".tif", src_dir, recurse=recurse)
    if not dest_dir.exists():
        dest_dir.mkdir(parents=True)
    elif clear_dir:
        clear_directory(dest_dir)

    def __exec__(path):
        if preserve_dir:
            relpath = path.relative_to(src_dir)
            dest_path = dest_dir / relpath.with_suffix(".png")
        else:
            dest_path = dest_dir / f"{path.name}.png"
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        _ = tiff_to_png(path, dest_path)
        file_map[path.stem] = {"tif": path, "png": dest_path}

    pbar = trange(len(src_paths), desc="Converting TIFF to PNG", leave=leave)
    with ThreadPoolExecutor(max_workers=get_cpu_count()) as executor:
        futures = [executor.submit(__exec__, path) for path in src_paths]
        for _ in as_completed(futures):
            pbar.update()

    if leave:
        pbar.set_description("Complete")
    pbar.close()

    return file_map


def chip_geotiff_and_convert_to_png(geotiff_path, *, chip_size):
    epsg_code = None
    with rasterio.open(geotiff_path) as src:
        if src.crs.is_epsg_code:
            # Returns a number indicating the EPSG code
            epsg_code = src.crs.to_epsg()

    if not epsg_code:
        raise AttributeError("GeoTIFF missing EPSG identifier ")

    images = []
    chips = create_chips_from_geotiff(geotiff_path, chip_size=chip_size, crs=src.crs)

    for tiff, coords in chips:
        image = tiff_to_png(tiff)
        if image.max() != float("nan"):
            images.append((image, coords))
    return images, epsg_code
