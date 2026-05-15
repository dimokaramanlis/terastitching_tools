#!/usr/bin/env python
"""
This script automatically generates a TeraStitcher XML descriptor and execution
script for MULTI-CHANNEL datasets using exact stage coordinates from CBF metadata.

Supports TWO folder structures:
  A) Single experiment: root/ has one .cbf and images/RAW_DATA/
  B) Multi experiment:  root/ has sub-folders, each with its own .cbf and images/RAW_DATA/
     (channels restart from c0 in each sub-folder; tile indices restart from image_xy0)

Author: DK & Gemini
Date: January 08, 2026 — Extended May 2026
"""
import os
import sys
import json
import re
import xml.etree.ElementTree as ET
from tkinter import Tk, filedialog
from xml.dom import minidom
from math import floor

# --- CONFIGURABLE PARAMETERS ---

# 1. USER OVERRIDES
VOXEL_SIZE_FALLBACK = {
    "H": 1.44,   # Voxel size in microns (X-axis / Horizontal) - Locked to empirically true value
    "V": 1.44,   # Voxel size in microns (Y-axis / Vertical)   - Locked to empirically true value
    "D": 5.0     # Z-spacing in microns
}

# 2. Metadata File Names
CBF_METADATA_FILENAME = ".cbf"
OME_METADATA_FILENAME = "metadata.companion.ome"

# 3. Tile Folder Naming Convention
TILE_FOLDER_FORMAT = "image_xy{index}"

# 4. TeraStitcher Execution Parameters
DEFAULT_STITCHING_PARAMS = {
    "sV": 25, "sH": 25, "sD": 20,
    "displ_threshold": 0.8,
    "imout_depth": 16, "subvoldim": 100
}
# --- END OF CONFIGURABLE PARAMETERS ---


def find_file_by_suffix(root_path, suffix):
    for item in os.listdir(root_path):
        if item.endswith(suffix): return os.path.join(root_path, item)
    return None


# ---------------------------------------------------------------------------
#  Experiment discovery: single-folder vs multi-folder
# ---------------------------------------------------------------------------

def discover_experiments(root_folder):
    """
    Detects folder structure and returns a list of experiment descriptors.

    Returns list of dicts:
        cbf_path, raw_data_folder, ome_path, subfolder_name (None for single)
    """
    experiments = []

    # Case 1: root itself is the experiment
    cbf = find_file_by_suffix(root_folder, CBF_METADATA_FILENAME)
    raw = os.path.join(root_folder, "images", "RAW_DATA")
    ome = os.path.join(raw, OME_METADATA_FILENAME)

    if cbf and os.path.isdir(raw) and os.path.exists(ome):
        return [{
            'cbf_path': cbf,
            'raw_data_folder': raw,
            'ome_path': ome,
            'subfolder_name': None,
        }]

    # Case 2: sub-folders are experiments
    for name in sorted(os.listdir(root_folder)):
        sub = os.path.join(root_folder, name)
        if not os.path.isdir(sub):
            continue
        cbf = find_file_by_suffix(sub, CBF_METADATA_FILENAME)
        raw = os.path.join(sub, "images", "RAW_DATA")
        ome = os.path.join(raw, OME_METADATA_FILENAME)
        if cbf and os.path.isdir(raw) and os.path.exists(ome):
            experiments.append({
                'cbf_path': cbf,
                'raw_data_folder': raw,
                'ome_path': ome,
                'subfolder_name': name,
            })

    return experiments


# ---------------------------------------------------------------------------
#  Low-level coordinate extraction (raw, before normalisation)
# ---------------------------------------------------------------------------

def extract_raw_coords_from_cbf(cbf_path):
    """
    Returns raw stage coordinates and mosaic step sizes from a CBF file,
    WITHOUT normalising to a grid origin.  Used for both single and multi modes.
    """
    with open(cbf_path, 'r', encoding='utf-8') as f:
        cbf_data = json.load(f)

    mosaic = None
    for item in cbf_data.get('mda', {}).get('list', []):
        if 'mosaic' in item:
            mosaic = item['mosaic']['MosaicElems'][0]
            break
    if not mosaic:
        return None

    positions = mosaic.get('Positions', [])
    coords = []
    for pos in positions:
        val_x, val_y = 0, 0
        for dev in pos.get('devices', []):
            dev_id = dev.get('Id', {}).get('SubDeviceId', '')
            if dev_id == 'Axis2AbsolutePosition':
                val_x = dev.get('Value', 0)
            elif dev_id == 'Axis1AbsolutePosition':
                val_y = dev.get('Value', 0)
        coords.append((val_x, val_y))

    step_x_nm = mosaic.get('UserOffsetX')
    if not step_x_nm:
        unique_x = sorted(set(c[0] for c in coords))
        step_x_nm = unique_x[1] - unique_x[0] if len(unique_x) > 1 else 1

    step_y_nm = mosaic.get('UserOffsetY')
    if not step_y_nm:
        unique_y = sorted(set(c[1] for c in coords))
        step_y_nm = unique_y[1] - unique_y[0] if len(unique_y) > 1 else 1

    return {
        'coords': coords,
        'step_x_nm': step_x_nm,
        'step_y_nm': step_y_nm,
        'grid_w_hint': mosaic.get('xTileNumber'),
        'grid_h_hint': mosaic.get('yTileNumber'),
    }


def normalise_coords(all_tiles_raw, params):
    """
    Takes a list of raw tile dicts (each with 'x', 'y', 'local_index',
    'data_folder', 'dir_name') and a params dict with voxel sizes / step.

    Returns (tile_data, grid_w, grid_h, mech_disp_H_microns, mech_disp_V_microns).
    """
    step_x_nm = all_tiles_raw[0]['step_x_nm']
    step_y_nm = all_tiles_raw[0]['step_y_nm']

    max_x = max(t['x'] for t in all_tiles_raw)
    max_y = max(t['y'] for t in all_tiles_raw)

    tile_data = []
    for t in all_tiles_raw:
        col = int(round((max_x - t['x']) / step_x_nm))
        row = int(round((max_y - t['y']) / step_y_nm))
        abs_h = (max_x - t['x']) * 1e-3 / params['voxel_H']
        abs_v = (max_y - t['y']) * 1e-3 / params['voxel_V']

        tile_data.append({
            'index': t['local_index'],
            'row': row,
            'col': col,
            'abs_h': int(round(abs_h)),
            'abs_v': int(round(abs_v)),
            'data_folder': t['data_folder'],
            'dir_name': t['dir_name'],
        })

    grid_w = max(t['col'] for t in tile_data) + 1
    grid_h = max(t['row'] for t in tile_data) + 1
    mech_H = step_x_nm * 1e-3
    mech_V = step_y_nm * 1e-3

    return tile_data, grid_w, grid_h, mech_H, mech_V


# ---------------------------------------------------------------------------
#  Original single-experiment coordinate extraction (wraps the above)
# ---------------------------------------------------------------------------

def extract_positions_from_cbf(cbf_data_path, params, data_folder):
    """Single-experiment extraction — backward-compatible wrapper."""
    raw = extract_raw_coords_from_cbf(cbf_data_path)
    if raw is None:
        return None

    tiles_raw = []
    for i, (cx, cy) in enumerate(raw['coords']):
        tiles_raw.append({
            'x': cx, 'y': cy,
            'local_index': i,
            'data_folder': data_folder,
            'dir_name': TILE_FOLDER_FORMAT.format(index=i),
            'step_x_nm': raw['step_x_nm'],
            'step_y_nm': raw['step_y_nm'],
        })

    return normalise_coords(tiles_raw, params)


# ---------------------------------------------------------------------------
#  Multi-experiment coordinate merging
# ---------------------------------------------------------------------------

def merge_experiments_positions(experiments, params, stacks_dir):
    """
    Extracts raw stage coordinates from every experiment's CBF, merges them
    into a single unified coordinate space, and returns the same tuple as
    extract_positions_from_cbf.

    Each tile's dir_name is set relative to *stacks_dir* so TeraStitcher
    can find it via stacks_dir + "/" + dir_name.
    """
    all_tiles_raw = []

    for exp in experiments:
        raw = extract_raw_coords_from_cbf(exp['cbf_path'])
        if raw is None:
            print(f"Error: Could not extract positions from {exp['cbf_path']}")
            return None

        for i, (cx, cy) in enumerate(raw['coords']):
            tile_folder = TILE_FOLDER_FORMAT.format(index=i)
            # dir_name must be relative to stacks_dir
            rel_path = os.path.relpath(
                os.path.join(exp['raw_data_folder'], tile_folder),
                stacks_dir
            )
            # Ensure forward slashes for XML (works on Windows too for TS)
            rel_path = rel_path.replace("\\", "/")

            all_tiles_raw.append({
                'x': cx, 'y': cy,
                'local_index': i,
                'data_folder': exp['raw_data_folder'],
                'dir_name': rel_path,
                'step_x_nm': raw['step_x_nm'],
                'step_y_nm': raw['step_y_nm'],
            })

    print(f"  - Merging {len(all_tiles_raw)} tiles from {len(experiments)} sub-experiments")
    return normalise_coords(all_tiles_raw, params)


# ---------------------------------------------------------------------------
#  OME / metadata parsing
# ---------------------------------------------------------------------------

def parse_ome_params(ome_path):
    """Extracts image dimensions and voxel sizes from the OME companion file."""
    params = {}
    try:
        tree = ET.parse(ome_path)
        root = tree.getroot()
        ns = {'ome': 'http://www.openmicroscopy.org/Schemas/OME/2016-06'}
        pixels_node = root.find('ome:Image/ome:Pixels', ns)

        def get_dim(attr, fallback):
            val = pixels_node.get(attr)
            if val is None: return fallback
            unit = pixels_node.get(attr + 'Unit', 'um')
            return float(val) * 1e-3 if unit == 'nm' else float(val)

        params['voxel_H'] = VOXEL_SIZE_FALLBACK["H"]
        params['voxel_V'] = VOXEL_SIZE_FALLBACK["V"]
        params['z_spacing'] = get_dim('PhysicalSizeZ', VOXEL_SIZE_FALLBACK["D"])
        params['no_slices_metadata'] = int(pixels_node.get('SizeZ'))
        params['img_width_px'] = int(pixels_node.get('SizeX'))
        params['img_height_px'] = int(pixels_node.get('SizeY'))
    except Exception as e:
        print(f"Error parsing OME: {e}")
        return None
    return params


def parse_metadata(cbf_path, ome_path, data_folder):
    """Full metadata parse for a SINGLE-experiment layout."""
    print("Parsing metadata...")
    params = parse_ome_params(ome_path)
    if params is None:
        return None

    try:
        extracted = extract_positions_from_cbf(cbf_path, params, data_folder)
        if not extracted:
            print("Error: Could not extract mosaic positions from CBF.")
            return None

        tile_data, grid_w, grid_h, mech_H, mech_V = extracted
        params.update({
            'tile_data': tile_data,
            'gridH': grid_w,
            'gridV': grid_h,
            'mech_disp_H_microns': mech_H,
            'mech_disp_V_microns': mech_V
        })

        print(f"  - Detected Voxel Size: {params['voxel_H']:.3f} um (Forced override)")
        print(f"  - Extracted {len(tile_data)} exact tile positions from stage coordinates.")

    except Exception as e:
        print(f"Error parsing CBF: {e}")
        return None

    return params


def parse_metadata_multi(experiments, stacks_dir):
    """Full metadata parse for a MULTI-experiment layout (≥2 sub-folders)."""
    print("Parsing metadata (multi-experiment mode)...")
    # Use the first experiment's OME for image dimensions / voxel sizes
    params = parse_ome_params(experiments[0]['ome_path'])
    if params is None:
        return None

    try:
        result = merge_experiments_positions(experiments, params, stacks_dir)
        if not result:
            return None

        tile_data, grid_w, grid_h, mech_H, mech_V = result
        params.update({
            'tile_data': tile_data,
            'gridH': grid_w,
            'gridV': grid_h,
            'mech_disp_H_microns': mech_H,
            'mech_disp_V_microns': mech_V
        })

        print(f"  - Detected Voxel Size: {params['voxel_H']:.3f} um (Forced override)")
        print(f"  - Merged {len(tile_data)} tile positions across {len(experiments)} experiments.")

    except Exception as e:
        print(f"Error during multi-experiment merge: {e}")
        return None

    return params


# ---------------------------------------------------------------------------
#  Channel descriptions from OME
# ---------------------------------------------------------------------------

def get_channel_descriptions(ome_path):
    """Parses OME XML to return a dictionary of channel ID -> descriptive string."""
    descriptions = {}
    try:
        tree = ET.parse(ome_path)
        root = tree.getroot()
        ns = {'ome': 'http://www.openmicroscopy.org/Schemas/OME/2016-06'}

        for channel in root.findall(".//ome:Channel", ns):
            full_id = channel.get('ID', '0:0')
            c_id = full_id.split(':')[-1]

            if c_id not in descriptions:
                name = channel.get('Name', 'Unknown')
                ex = channel.get('ExcitationWavelength', '?')
                em = channel.get('EmissionWavelength', '?')
                descriptions[c_id] = f"{name}nm | Ex: {ex}nm / Em: {em}nm"

        processed_params = set()
        for map_ann in root.findall(".//ome:MapAnnotation", ns):
            ann_id = map_ann.get('ID', '')
            if 'ChannelParameters' in ann_id and ann_id not in processed_params:
                c_idx = ann_id.replace('ChannelParameters', '')
                if c_idx in descriptions:
                    values = {m.get('K'): m.text for m in map_ann.findall(".//ome:M", ns)}
                    power = next((v for k, v in values.items() if 'Power (%)' in k), "N/A")
                    filt = values.get('Emission_FW-Filters', "N/A")
                    descriptions[c_idx] = f"{descriptions[c_idx]} | Power: {power}% | Filter: {filt}"
                    processed_params.add(ann_id)

    except Exception as e:
        print(f"Warning: Could not parse channel details: {e}")
    return descriptions


def merge_channel_descriptions(experiments):
    """Merge channel descriptions from all experiments (take union, first wins)."""
    merged = {}
    for exp in experiments:
        descs = get_channel_descriptions(exp['ome_path'])
        for k, v in descs.items():
            if k not in merged:
                merged[k] = v
    return merged


# ---------------------------------------------------------------------------
#  Channel detection & slice verification — now tile-data-aware
# ---------------------------------------------------------------------------

def get_available_channels(params):
    """
    Scans tile folders (using the enriched tile_data with data_folder) to
    detect which channels (c0, c1...) are present.
    """
    channels = set()
    pattern = re.compile(r"_c(\d+)_z")
    tile_data = params['tile_data']
    tiles_to_check = min(5, len(tile_data))

    for tile in tile_data[:tiles_to_check]:
        tile_path = os.path.join(tile['data_folder'],
                                 TILE_FOLDER_FORMAT.format(index=tile['index']))
        if os.path.isdir(tile_path):
            for f in os.listdir(tile_path):
                match = pattern.search(f)
                if match:
                    channels.add(int(match.group(1)))

    sorted_channels = sorted(list(channels))
    if not sorted_channels:
        return [0]
    return sorted_channels


def verify_slice_counts(params, channel_idx):
    """Counts actual files for a specific channel across all tiles."""
    print(f"  - Verifying slice counts for Channel {channel_idx}...")
    min_slices_found = float('inf')
    chan_pattern = f"_c{channel_idx}_z"
    tile_data = params['tile_data']

    for tile in tile_data:
        tile_path = os.path.join(tile['data_folder'],
                                 TILE_FOLDER_FORMAT.format(index=tile['index']))
        if os.path.isdir(tile_path):
            try:
                files = [f for f in os.listdir(tile_path)
                         if f.endswith('.ome.tif') and chan_pattern in f]
                file_count = len(files)
                if file_count < min_slices_found:
                    min_slices_found = file_count
            except OSError:
                continue

    if min_slices_found == float('inf') or min_slices_found == 0:
        print(f"    - Error: No images found for Channel {channel_idx}.")
        return 0
    return min_slices_found


# ---------------------------------------------------------------------------
#  XML generation
# ---------------------------------------------------------------------------

def generate_terastitcher_xml(output_path, params, stacks_dir, channel_idx, slice_count):
    """
    Generates the TeraStitcher import XML.

    stacks_dir  – the base directory written into <stacks_dir value="...">
    Each tile's DIR_NAME is taken from tile['dir_name'] (already relative to
    stacks_dir for both single and multi modes).
    """
    print(f"Generating XML for Channel {channel_idx} -> {os.path.basename(output_path)}")

    vxl_V = params['voxel_V']
    vxl_H = params['voxel_H']
    vxl_D = params['z_spacing']

    channel_regex = f"_c{channel_idx}_z.*\\.ome\\.tif"

    terastitcher_node = ET.Element('TeraStitcher')
    terastitcher_node.set('volume_format', 'TiledXY|2Dseries')
    terastitcher_node.set('input_plugin', 'tiff2D')

    ET.SubElement(terastitcher_node, 'stacks_dir', value=str(stacks_dir))
    ET.SubElement(terastitcher_node, 'mdata_bin',
                  value=os.path.join(stacks_dir, f'mdata_c{channel_idx}.bin'))
    ET.SubElement(terastitcher_node, 'ref_sys', ref1="1", ref2="2", ref3="3")
    ET.SubElement(terastitcher_node, 'voxel_dims', V=str(vxl_V), H=str(vxl_H), D=str(vxl_D))
    ET.SubElement(terastitcher_node, 'origin', V="0", H="0", D="0")
    ET.SubElement(terastitcher_node, 'mechanical_displacements',
                  V=str(params['mech_disp_V_microns']),
                  H=str(params['mech_disp_H_microns']))

    ET.SubElement(terastitcher_node, 'dimensions',
                  stack_rows=str(params['gridV']),
                  stack_columns=str(params['gridH']),
                  stack_slices=str(slice_count))

    stacks_node = ET.SubElement(terastitcher_node, 'STACKS')

    sorted_tiles = sorted(params['tile_data'], key=lambda t: (t['row'], t['col']))

    for tile in sorted_tiles:
        dir_name = tile['dir_name']

        stack_attribs = {
            'N_CHANS': "1",
            'N_BYTESxCHAN': "2",
            'ROW': str(tile['row']), 'COL': str(tile['col']),
            'ABS_V': str(tile['abs_v']),
            'ABS_H': str(tile['abs_h']),
            'ABS_D': "0",
            'STITCHABLE': "no",
            'DIR_NAME': dir_name,
            'IMG_REGEX': channel_regex,
            'Z_RANGES': f"[{0},{slice_count})"
        }
        stack_node = ET.SubElement(stacks_node, 'Stack', **stack_attribs)

        for direction in ['NORTH', 'EAST', 'SOUTH', 'WEST']:
            ET.SubElement(stack_node, f'{direction}_displacements')

    xml_string = ET.tostring(terastitcher_node, 'utf-8')
    reparsed = minidom.parseString(xml_string)
    pretty_xml = reparsed.toprettyxml(indent="  ", encoding="UTF-8")

    with open(output_path, 'wb') as f:
        f.write(pretty_xml)


# ---------------------------------------------------------------------------
#  Propagation script & execution script
# ---------------------------------------------------------------------------

def create_propagation_script(output_folder, main_channel_idx, available_channels):
    """
    Creates a Python script that CLONES the aligned reference XML and
    PATCHES it for all other channels (updating mdata path and regex).
    """
    script_path = os.path.join(output_folder, "propagate_xmls.py")

    content = f"""
import xml.etree.ElementTree as ET
import os
import sys

# AUTO-GENERATED SCRIPT to propagate TeraStitcher alignments
MAIN_CHANNEL = {main_channel_idx}
CHANNELS = {available_channels}

def main():
    print("--- Running XML Propagation Step ---")
    aligned_xml = f"xml_merging_c{{MAIN_CHANNEL}}.xml"

    if not os.path.exists(aligned_xml):
        print(f"Error: Aligned file {{aligned_xml}} not found! Alignment failed?")
        sys.exit(1)

    print(f"Cloning alignment from reference: {{aligned_xml}}")

    for c_idx in CHANNELS:
        if c_idx == MAIN_CHANNEL:
            continue

        target_xml = f"xml_merging_c{{c_idx}}.xml"

        try:
            tree = ET.parse(aligned_xml)
            root = tree.getroot()

            mdata_node = root.find("mdata_bin")
            if mdata_node is not None:
                old_val = mdata_node.get("value")
                new_val = old_val.replace(f"mdata_c{{MAIN_CHANNEL}}.bin", f"mdata_c{{c_idx}}.bin")
                mdata_node.set("value", new_val)

            old_regex_part = f"_c{{MAIN_CHANNEL}}_z"
            new_regex_part = f"_c{{c_idx}}_z"

            stacks = root.findall(".//Stack")
            print(f"  -> Generating {{target_xml}} (Updating {{len(stacks)}} stacks)...")

            for stack in stacks:
                regex = stack.get("IMG_REGEX")
                if regex and old_regex_part in regex:
                    stack.set("IMG_REGEX", regex.replace(old_regex_part, new_regex_part))
                stack.set("STITCHABLE", "no")

            tree.write(target_xml)
            print(f"     [OK] Saved {{target_xml}}")

        except Exception as e:
            print(f"Error processing channel {{c_idx}}: {{e}}")
            sys.exit(1)

    print("Propagation complete.")

if __name__ == "__main__":
    main()
"""
    with open(script_path, 'w') as f:
        f.write(content)


def generate_execution_script(output_folder, available_channels, main_channel_idx):
    """Generates the batch script following the official MultiChannel alignment workflow."""
    is_windows = sys.platform.startswith('win')
    script_filename = f"run_stitching_multi.{'bat' if is_windows else 'sh'}"
    script_path = os.path.join(output_folder, script_filename)

    main_import_xml = f"terastitcher_import_c{main_channel_idx}.xml"
    main_merging_xml = f"xml_merging_c{main_channel_idx}.xml"

    imout_depth = DEFAULT_STITCHING_PARAMS["imout_depth"]
    subvoldim = DEFAULT_STITCHING_PARAMS["subvoldim"]
    sV = DEFAULT_STITCHING_PARAMS["sV"]
    sH = DEFAULT_STITCHING_PARAMS["sH"]
    sD = DEFAULT_STITCHING_PARAMS["sD"]
    thres = DEFAULT_STITCHING_PARAMS["displ_threshold"]

    lines = []

    lines.append("@echo off")
    lines.append(f"echo --- Phase 1: Generating Test Projections for ALL channels ---")

    for c in available_channels:
        lines.append(f"echo Generating test image for Channel {c}...")
        lines.append(f'terastitcher --test --projin="terastitcher_import_c{c}.xml" --imout_depth={imout_depth} --sparse_data')
        lines.append(f'if exist "test_middle_slice.tif" ren "test_middle_slice.tif" "test_c{c}.tif"')

    lines.append(f"echo.")
    lines.append(f"echo ----------------------------------------------------------------")
    lines.append(f"echo Please check the generated 'test_cX.tif' images in the folder.")
    lines.append(f"echo ----------------------------------------------------------------")
    lines.append('SET /P continue="Are the projections correct (y/n)? "')
    lines.append('IF /I "%continue%" NEQ "y" EXIT /B 1')

    lines.append(f"echo.")
    lines.append(f"echo --- Phase 2: Aligning Reference Channel {main_channel_idx} ---")
    lines.append(f'terastitcher --displcompute --projin="{main_import_xml}" --projout="xml_comp.xml" --subvoldim={subvoldim} --sV={sV} --sH={sH} --sD={sD} --sparse_data')
    lines.append("IF %ERRORLEVEL% NEQ 0 GOTO ERROR")

    lines.append(f'terastitcher --displproj --projin="xml_comp.xml" --projout="xml_proj.xml" --sparse_data')
    lines.append("IF %ERRORLEVEL% NEQ 0 GOTO ERROR")

    lines.append(f'terastitcher --displthres --projin="xml_proj.xml" --projout="xml_thres.xml" --threshold={thres} --sparse_data')
    lines.append("IF %ERRORLEVEL% NEQ 0 GOTO ERROR")

    lines.append(f'terastitcher --placetiles --projin="xml_thres.xml" --projout="{main_merging_xml}" --sparse_data')
    lines.append("IF %ERRORLEVEL% NEQ 0 GOTO ERROR")

    lines.append("\n" + f"echo --- Phase 3: Propagating Alignment to Satellite Channels ---")
    lines.append(f'{sys.executable} propagate_xmls.py')
    lines.append("IF %ERRORLEVEL% NEQ 0 GOTO ERROR")

    lines.append("\n" + f"echo --- Phase 4: Merging Channels Independently ---")
    for c_idx in available_channels:
        input_xml = f"xml_merging_c{c_idx}.xml"
        output_dir = f"Stitched_c{c_idx}"
        lines.append(f'echo Merging Channel {c_idx}...')
        lines.append(f'if not exist "{output_dir}" mkdir "{output_dir}"')
        lines.append(f'terastitcher --merge --projin="{input_xml}" --volout="{output_dir}" --resolutions=024 --imout_depth={imout_depth} --sparse_data')
        lines.append("IF %ERRORLEVEL% NEQ 0 GOTO ERROR")

    lines.append("\necho Stitching Completed Successfully!")
    lines.append("PAUSE")
    lines.append("EXIT /B 0")

    lines.append("\n:ERROR")
    lines.append("echo Pipeline failed.")
    lines.append("PAUSE")
    lines.append("EXIT /B 1")

    with open(script_path, 'w', newline='\r\n' if is_windows else '\n') as f:
        f.write("\n".join(lines))

    if not is_windows:
        os.chmod(script_path, 0o755)


# ---------------------------------------------------------------------------
#  File renaming (zero-padding)
# ---------------------------------------------------------------------------

def check_and_rename_files(data_folders):
    """
    Renames files to ensure zero-padding.
    Accepts a single path (str) or a list of paths.
    """
    if isinstance(data_folders, str):
        data_folders = [data_folders]

    rename_pattern = re.compile(r"(_c\d+_z)(\d+)(\.ome\.tif)$")
    files_to_process = []
    max_z_number = 0

    print("Scanning folders for padding issues...")
    for data_folder in data_folders:
        for root_dir, _, files in os.walk(data_folder):
            for filename in files:
                match = rename_pattern.match(filename)
                if match:
                    prefix, number_str, suffix = match.groups()
                    number = int(number_str)
                    max_z_number = max(max_z_number, number)
                    files_to_process.append({
                        "path": os.path.join(root_dir, filename),
                        "dir": root_dir,
                        "filename": filename,
                        "prefix": prefix,
                        "number_str": number_str,
                        "suffix": suffix
                    })

    if not files_to_process:
        return True

    padding_width = max(4, len(str(max_z_number)))
    files_to_rename = [f for f in files_to_process if len(f['number_str']) < padding_width]

    if not files_to_rename:
        return True

    print(f"\n⚠️ WARNING: Found {len(files_to_rename)} files to rename (Padding to {padding_width} digits).")
    try:
        user_input = input("Proceed with renaming? (y/n): ").lower()
    except EOFError:
        user_input = 'n'

    if user_input != 'y':
        return False

    print("Renaming files...")
    for f in files_to_rename:
        new_number_str = f['number_str'].zfill(padding_width)
        new_filename = f"{f['prefix']}{new_number_str}{f['suffix']}"
        try:
            os.rename(f['path'], os.path.join(f['dir'], new_filename))
        except OSError:
            pass
    return True


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------

def main():
    root = Tk()
    root.withdraw()
    selected_folder = filedialog.askdirectory(title="Select the ROOT folder of your experiment")

    if not selected_folder:
        return

    print(f"Selected data folder: {selected_folder}")

    # ---- Discover experiment structure ----
    experiments = discover_experiments(selected_folder)
    if not experiments:
        print("Error: Could not find any experiment folders with CBF + images/RAW_DATA.")
        print("Expected either:")
        print("  A) A .cbf file at root level with images/RAW_DATA/ subfolder")
        print("  B) Sub-folders, each containing a .cbf and images/RAW_DATA/")
        return

    is_single = (len(experiments) == 1 and experiments[0]['subfolder_name'] is None)

    if is_single:
        print("Detected: SINGLE experiment folder")
    else:
        names = [e['subfolder_name'] for e in experiments]
        print(f"Detected: MULTI experiment folders ({len(experiments)}): {names}")

    # ---- Determine output folder and stacks_dir ----
    if is_single:
        stacks_dir = experiments[0]['raw_data_folder']
        output_folder = stacks_dir
    else:
        # For multi-folder: everything is relative to the selected root
        stacks_dir = selected_folder
        output_folder = selected_folder

    # ---- Rename check across all RAW_DATA folders ----
    all_raw_folders = [e['raw_data_folder'] for e in experiments]
    check_and_rename_files(all_raw_folders)

    # ---- Parse metadata ----
    if is_single:
        exp = experiments[0]
        params = parse_metadata(exp['cbf_path'], exp['ome_path'], exp['raw_data_folder'])
        channel_details = get_channel_descriptions(exp['ome_path'])
    else:
        params = parse_metadata_multi(experiments, stacks_dir)
        channel_details = merge_channel_descriptions(experiments)

    if not params:
        return

    # ---- Detect channels ----
    available_channels = get_available_channels(params)
    print("\n--- Available Channels Detected ---")
    for c_id in sorted(available_channels):
        info = channel_details.get(str(c_id), "No metadata found")
        print(f"[{c_id}]: {info}")

    # ---- Select main alignment channel ----
    selected_channel = None
    if len(available_channels) == 1:
        selected_channel = available_channels[0]
        print(f"Only one channel detected ({selected_channel}). Selecting automatically.")
    else:
        while True:
            try:
                user_input = input(f"Enter the MAIN channel for alignment {available_channels}: ")
                selection = int(user_input)
                if selection in available_channels:
                    selected_channel = selection
                    break
                else:
                    print("Invalid selection.")
            except ValueError:
                pass

    print(f"\n--- Generating Files for Main Channel {selected_channel} and Satellites ---")

    # ---- Generate Import XMLs for ALL channels ----
    for c_idx in available_channels:
        slice_count = verify_slice_counts(params, c_idx)
        xml_path = os.path.join(output_folder, f"terastitcher_import_c{c_idx}.xml")
        generate_terastitcher_xml(xml_path, params, stacks_dir, c_idx, slice_count)

    # ---- Generate helper scripts ----
    create_propagation_script(output_folder, selected_channel, available_channels)

    generate_execution_script(
        output_folder,
        available_channels,
        selected_channel)

    print("\nProcess finished successfully!")
    if is_single:
        target = "the RAW_DATA folder"
    else:
        target = f"the root folder ({os.path.basename(selected_folder)})"

    if sys.platform.startswith('win'):
        print(f"Run 'run_stitching_multi.bat' inside {target}.")
    else:
        print(f"Run './run_stitching_multi.sh' inside {target}.")


if __name__ == '__main__':
    main()
