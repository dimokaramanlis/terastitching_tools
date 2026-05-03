#!/usr/bin/env python
"""
This script automatically generates a TeraStitcher XML descriptor and execution
script for a SINGLE selected channel.

CORRECTIONS (2025-12-03):
1. Fixed tiling inaccuracies by calculating step sizes directly from stage coordinate deltas.
2. Removed legacy 240px dimension constraints; now uses actual image dimensions (2048px).
3. Enforces Row-Major indexing (Index 0=Top-Left, Index 1=Right) by dynamically mapping 
   stage axes to image axes.
4. Solved ambiguity where "20% overlap" effectively meant "10% intersection".

Author: DK & Gemini
Date: December 03, 2025
"""
import os
import sys
import json
import re
import xml.etree.ElementTree as ET
from tkinter import Tk, filedialog
from xml.dom import minidom
from math import floor, sqrt

# --- CONFIGURABLE PARAMETERS ---

# 1. USER OVERRIDES
VOXEL_SIZE_OVERRIDE = {
    "H": 1.44,  # Voxel size in microns (X-axis / Horizontal)
    "V": 1.44,  # Voxel size in microns (Y-axis / Vertical)
    "D": 5.0    # Z-spacing in microns
}

# 2. Metadata File Names
CBF_METADATA_FILENAME = ".cbf"
OME_METADATA_FILENAME = "metadata.companion.ome"

# 3. Tile Folder Naming Convention
TILE_FOLDER_FORMAT = "image_xy{index}"

# 4. TeraStitcher Execution Parameters
DEFAULT_STITCHING_PARAMS = {
    "sV": 20, "sH": 20, "sD": 20,
    "displ_threshold": 0.8,
    "imout_depth": 16, "subvoldim": 100
}
# --- END OF CONFIGURABLE PARAMETERS ---


def find_file_by_suffix(root_path, suffix):
    """Finds the first file in a directory with a given suffix."""
    for item in os.listdir(root_path):
        if item.endswith(suffix):
            return os.path.join(root_path, item)
    return None

def calculate_steps_from_coordinates(cbf_data, voxel_size_h, voxel_size_v):
    """
    Calculates tile step sizes in pixels and builds an explicit (row, col) mapping
    for every folder index, based on stage coordinates.

    Stage convention (Inscoper):
      Axis1 (X) -> Image Horizontal axis (columns).  Largest X = column 0.
      Axis2 (Y) -> Image Vertical axis   (rows).     Largest Y = row 0.

    Returns: step_pixels_h, step_pixels_v, grid_cols, grid_rows, tile_layout
      tile_layout[i] = (row, col) for folder image_xy{i}.
    """
    print("\n--- Calculating Precise Steps from Stage Coordinates ---")

    try:
        mosaic = cbf_data['mda']['list'][0]['mosaic']['MosaicElems'][0]
        positions_list = mosaic['Positions']

        if len(positions_list) < 2:
            print("  - Warning: Not enough positions. Defaulting to 10% overlap.")
            return 2048 * 0.9, 2048 * 0.9, 1, 1, [(0, 0)]

        # 1. Extract Axis1 (X) and Axis2 (Y) stage coordinates for every position.
        coords = []
        for pos in positions_list:
            x = 0.0
            y = 0.0
            for device in pos['devices']:
                sid = device['Id'].get('SubDeviceId', '')
                if 'Axis1AbsolutePosition' in sid:
                    x = float(device['Value'])
                elif 'Axis2AbsolutePosition' in sid:
                    y = float(device['Value'])
            coords.append((x, y))

        # 2. Derive the grid layout directly from unique coordinate values.
        #    Round to the nearest 100 nm to absorb stage jitter.
        #    Axis1 (X) -> columns: sort descending so the most-positive X is col 0.
        #    Axis2 (Y) -> rows:    sort descending so the most-positive Y is row 0.
        unique_x = sorted(set(round(c[0], -2) for c in coords), reverse=True)
        unique_y = sorted(set(round(c[1], -2) for c in coords), reverse=True)

        grid_cols = len(unique_x)
        grid_rows = len(unique_y)
        print(f"  - Grid Dimensions Detected: {grid_cols} Cols x {grid_rows} Rows")

        # 3. Calculate average step in stage units (nm) along each axis.
        if grid_cols > 1:
            steps_x = [abs(unique_x[i] - unique_x[i + 1]) for i in range(grid_cols - 1)]
            avg_step_x = sum(steps_x) / len(steps_x)
        else:
            avg_step_x = 0

        if grid_rows > 1:
            steps_y = [abs(unique_y[i] - unique_y[i + 1]) for i in range(grid_rows - 1)]
            avg_step_y = sum(steps_y) / len(steps_y)
        else:
            avg_step_y = 0

        # 4. Convert nm -> µm -> pixels.
        step_pixels_h = (avg_step_x * 1e-3) / voxel_size_h if avg_step_x else 2048 * 0.9
        step_pixels_v = (avg_step_y * 1e-3) / voxel_size_v if avg_step_y else 2048 * 0.9

        print(f"  - Calculated Step H: {step_pixels_h:.2f} pixels ({avg_step_x * 1e-3:.2f} um)")
        print(f"  - Calculated Step V: {step_pixels_v:.2f} pixels ({avg_step_y * 1e-3:.2f} um)")

        # 5. Build (row, col) for every folder index using the coordinate lookup.
        x_to_col = {x: i for i, x in enumerate(unique_x)}
        y_to_row = {y: i for i, y in enumerate(unique_y)}

        tile_layout = []
        for i, (x, y) in enumerate(coords):
            col = x_to_col[round(x, -2)]
            row = y_to_row[round(y, -2)]
            tile_layout.append((row, col))

        return step_pixels_h, step_pixels_v, grid_cols, grid_rows, tile_layout

    except Exception as e:
        print(f"  - Error during coordinate calculation: {e}")
        print("  - Fallback: Using default theoretical overlap (10%).")
        fallback_n = 25
        return 2048 * 0.9, 2048 * 0.9, 5, 5, [(i // 5, i % 5) for i in range(fallback_n)]


def parse_metadata(cbf_path, ome_path):
    """Parses metadata files to extract experiment parameters."""
    params = {}
    print("Parsing metadata...")

    # --- Step 1: Read OME-XML first to get authoritative voxel sizes ---
    voxel_h = VOXEL_SIZE_OVERRIDE["H"]
    voxel_v = VOXEL_SIZE_OVERRIDE["V"]
    img_width_ome  = 2048
    img_height_ome = 2048
    try:
        tree = ET.parse(ome_path)
        root = tree.getroot()
        ns = {'ome': 'http://www.openmicroscopy.org/Schemas/OME/2016-06'}
        pixels_node = root.find('ome:Image/ome:Pixels', ns)
        if pixels_node is None:
            raise ValueError("Pixels node not found in OME-XML.")

        params['no_slices_metadata'] = int(pixels_node.get('SizeZ'))

        # Image pixel dimensions
        if pixels_node.get('SizeX'):
            img_width_ome  = int(pixels_node.get('SizeX'))
        if pixels_node.get('SizeY'):
            img_height_ome = int(pixels_node.get('SizeY'))

        # Physical voxel sizes — prefer OME-XML over the hardcoded override
        unit_to_um = {'nm': 1e-3, 'um': 1.0, 'µm': 1.0, 'mm': 1e3}
        phys_x_str  = pixels_node.get('PhysicalSizeX')
        phys_y_str  = pixels_node.get('PhysicalSizeY')
        phys_x_unit = pixels_node.get('PhysicalSizeXUnit', 'um')
        phys_y_unit = pixels_node.get('PhysicalSizeYUnit', 'um')

        if phys_x_str and phys_y_str:
            voxel_h = float(phys_x_str) * unit_to_um.get(phys_x_unit, 1.0)
            voxel_v = float(phys_y_str) * unit_to_um.get(phys_y_unit, 1.0)
            print(f"  - Voxel size from OME-XML: H={voxel_h:.4f} µm, V={voxel_v:.4f} µm")
        else:
            print(f"  - PhysicalSize not in OME-XML. Using override: {voxel_h} µm")

    except Exception as e:
        print(f"Error parsing OME-XML file '{ome_path}': {e}")
        return None

    # --- Step 2: Read CBF and calculate tile layout with correct voxel sizes ---
    try:
        with open(cbf_path, 'r', encoding='utf-8') as f:
            cbf_data = json.load(f)

        # Image pixel dimensions from CBF (fall back to OME-XML values)
        img_width  = img_width_ome
        img_height = img_height_ome
        try:
            for cam in cbf_data.get('ROIs', {}).get('PresetChannel', []):
                for dev in cam.get('devices', []):
                    sid = dev.get('Id', {}).get('SubDeviceId', '')
                    if sid == 'IMAGE WIDTH':
                        img_width  = int(dev['Value'])
                    elif sid == 'IMAGE HEIGHT':
                        img_height = int(dev['Value'])
        except Exception:
            pass

        params['img_width_px']  = img_width
        params['img_height_px'] = img_height

        # Calculate tile steps and layout using the correct voxel sizes
        step_h_px, step_v_px, grid_w, grid_h, tile_layout = calculate_steps_from_coordinates(
            cbf_data, voxel_h, voxel_v
        )

        params['step_H_px']    = floor(step_h_px)
        params['step_V_px']    = floor(step_v_px)
        params['gridH']        = grid_w
        params['gridV']        = grid_h
        params['tile_layout']  = tile_layout

        ov_h = 1.0 - (step_h_px / img_width)
        print(f"  - Effective Overlap H: {ov_h * 100:.2f}%")

    except Exception as e:
        print(f"Error parsing CBF file '{cbf_path}': {e}")
        return None

    params['voxel_H']   = voxel_h
    params['voxel_V']   = voxel_v
    params['z_spacing'] = VOXEL_SIZE_OVERRIDE["D"]

    return params


def get_available_channels(data_folder, params):
    """Scans the folder to detect which channels (c0, c1...) are present."""
    channels = set()
    pattern = re.compile(r"_c(\d+)_z")
    # Check first few tiles
    tiles_to_check = min(5, params['gridH'] * params['gridV'])
    
    for i in range(tiles_to_check):
        tile_path = os.path.join(data_folder, TILE_FOLDER_FORMAT.format(index=i))
        if os.path.isdir(tile_path):
            for f in os.listdir(tile_path):
                match = pattern.search(f)
                if match:
                    channels.add(int(match.group(1)))
    
    sorted_channels = sorted(list(channels))
    if not sorted_channels: return [0]
    return sorted_channels


def verify_slice_counts(data_folder, params, channel_idx):
    """Counts actual files for a specific channel."""
    print(f"  - Verifying slice counts for Channel {channel_idx}...")
    min_slices_found = float('inf')
    chan_pattern = f"_c{channel_idx}_z"
    
    total_tiles = params['gridH'] * params['gridV']
    
    for i in range(total_tiles):
        tile_folder_name = TILE_FOLDER_FORMAT.format(index=i)
        tile_path = os.path.join(data_folder, tile_folder_name)
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

def generate_terastitcher_xml(output_path, params, data_folder, channel_idx, slice_count):
    """Generates the TeraStitcher import XML."""
    print(f"Generating XML for Channel {channel_idx} -> {os.path.basename(output_path)}")

    # Mechanical displacements in the XML header must be in MICRONS (physical units).
    mech_disp_H_microns = params['step_H_px'] * params['voxel_H']
    mech_disp_V_microns = params['step_V_px'] * params['voxel_V']

    # Voxel dimensions in microns
    vxl_V = params['voxel_V']
    vxl_H = params['voxel_H']
    vxl_D = params['z_spacing']

    channel_regex = f"_c{channel_idx}_z.*\\.ome\\.tif"

    terastitcher_node = ET.Element('TeraStitcher')
    terastitcher_node.set('volume_format', 'TiledXY|2Dseries')
    terastitcher_node.set('input_plugin', 'tiff2D')

    ET.SubElement(terastitcher_node, 'stacks_dir', value=str(data_folder))
    ET.SubElement(terastitcher_node, 'mdata_bin', value=os.path.join(data_folder, f'mdata_c{channel_idx}.bin'))
    
    # Reference system: 1=V(Y), 2=H(X), 3=D(Z).
    ET.SubElement(terastitcher_node, 'ref_sys', ref1="1", ref2="2", ref3="3")
    
    ET.SubElement(terastitcher_node, 'voxel_dims', V=str(vxl_V), H=str(vxl_H), D=str(vxl_D))
    ET.SubElement(terastitcher_node, 'origin', V="0", H="0", D="0")
    
    ET.SubElement(terastitcher_node, 'mechanical_displacements', V=str(mech_disp_V_microns), H=str(mech_disp_H_microns))
    
    ET.SubElement(terastitcher_node, 'dimensions',
               stack_rows=str(params['gridV']),
               stack_columns=str(params['gridH']),
               stack_slices=str(slice_count))

    stacks_node = ET.SubElement(terastitcher_node, 'STACKS')

    # Build reverse lookup: (row, col) -> folder index, from the stage-coordinate mapping.
    layout_map = {(r, c): idx for idx, (r, c) in enumerate(params['tile_layout'])}

    for row in range(params['gridV']):
        for col in range(params['gridH']):

            # ABS_V and ABS_H must be in pixels.
            abs_H_eff = col * params['step_H_px']
            abs_V_eff = row * params['step_V_px']

            folder_index = layout_map.get((row, col))
            if folder_index is None:
                continue  # skip absent tiles in a sparse grid
            dir_name = TILE_FOLDER_FORMAT.format(index=folder_index)

            stack_attribs = {
                'N_CHANS': "1",
                'N_BYTESxCHAN': "2",
                'ROW': str(row), 'COL': str(col), 
                'ABS_V': str(abs_V_eff),
                'ABS_H': str(abs_H_eff), 
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

def generate_execution_script(output_folder, xml_import_file, channel_idx):
    """Generates a dedicated execution script for a specific channel."""
    is_windows = sys.platform.startswith('win')
    script_filename = f"run_stitching_c{channel_idx}.{'bat' if is_windows else 'sh'}"
    script_path = os.path.join(output_folder, script_filename)
    
    suffix = f"_c{channel_idx}"
    proj_displcomp = f"xml_displcomp{suffix}.xml"
    proj_displproj = f"xml_displproj{suffix}.xml"
    proj_displthres = f"xml_displthres{suffix}.xml"
    proj_merging = f"xml_merging{suffix}.xml"
    stitched_folder = os.path.join(output_folder, f"Stitched_c{channel_idx}")
    
    terastitcher_cmd = "terastitcher"
    comment_char = "REM" if is_windows else "#"

    lines = ["@echo off"] if is_windows else ["#!/bin/bash", "set -e"]
    lines.extend([
        "",
        f"{comment_char} TeraStitcher Pipeline for CHANNEL {channel_idx}",
        f"{comment_char} 1. Test import.",
        f'{terastitcher_cmd} --test --projin="{xml_import_file}" --imout_depth={DEFAULT_STITCHING_PARAMS["imout_depth"]} --sparse_data',
        ""
    ])
    
    if is_windows:
        lines.extend([
            'SET /P continue="Is the projection correct (y/n)?"',
            'IF NOT %continue% == y EXIT /B 1'
        ])
    else:
        lines.extend([
            'read -p "Is the projection correct (y/n)? " continue',
            'if [[ ! "$continue" =~ ^[Yy]$ ]]; then exit 1; fi'
        ])
    
    lines.extend([
        "",
        f"{comment_char} 2. Compute displacements",
        f'{terastitcher_cmd} --displcompute --projin="{xml_import_file}" --projout="{proj_displcomp}" --subvoldim={DEFAULT_STITCHING_PARAMS["subvoldim"]} --sV={DEFAULT_STITCHING_PARAMS["sV"]} --sH={DEFAULT_STITCHING_PARAMS["sH"]} --sD={DEFAULT_STITCHING_PARAMS["sD"]} --sparse_data',
        "",
        f"{comment_char} 3. Project displacements",
        f'{terastitcher_cmd} --displproj --projin="{proj_displcomp}" --projout="{proj_displproj}" --sparse_data',
        "",
        f"{comment_char} 4. Threshold displacements",
        f'{terastitcher_cmd} --displthres --projin="{proj_displproj}" --projout="{proj_displthres}" --threshold={DEFAULT_STITCHING_PARAMS["displ_threshold"]} --sparse_data',
        "",
        f"{comment_char} 5. Place tiles",
        f'{terastitcher_cmd} --placetiles --projin="{proj_displthres}" --projout="{proj_merging}" --sparse_data',
        "",
        f"{comment_char} 6. Merge tiles into final volume",
        f'mkdir "{stitched_folder}"',
        f'{terastitcher_cmd} --merge --projin="{proj_merging}" --volout="{stitched_folder}" --resolutions=024 --imout_depth={DEFAULT_STITCHING_PARAMS["imout_depth"]} --sparse_data',
        "",
        f"echo Stitching for Channel {channel_idx} complete!"
    ])

    with open(script_path, 'w', newline='\r\n' if is_windows else '\n') as f:
        f.write("\n".join(lines))
        
    if not is_windows: os.chmod(script_path, 0o755)


def check_and_rename_files(data_folder):
    """Renames files to ensure zero-padding."""
    print("\n--- Checking for filename sorting issues ---")
    rename_pattern = re.compile(r"(_c\d+_z)(\d+)(\.ome\.tif)$")
    files_to_process = []
    max_z_number = 0

    print("Scanning folders for padding issues...")
    for root, _, files in os.walk(data_folder):
        for filename in files:
            match = rename_pattern.match(filename)
            if match:
                prefix, number_str, suffix = match.groups()
                number = int(number_str)
                max_z_number = max(max_z_number, number)
                files_to_process.append({
                    "path": os.path.join(root, filename),
                    "dir": root,
                    "filename": filename,
                    "prefix": prefix,
                    "number_str": number_str,
                    "suffix": suffix
                })

    if not files_to_process:
        print("No image files found matching pattern.")
        return True

    padding_width = max(4, len(str(max_z_number)))
    files_to_rename = [f for f in files_to_process if len(f['number_str']) < padding_width]

    if not files_to_rename:
        print("✅ All filenames are correctly padded.")
        return True

    print(f"\n⚠️ WARNING: Found {len(files_to_rename)} files to rename (Padding to {padding_width} digits).")
    try:
        user_input = input("Proceed with renaming? (y/n): ").lower()
    except EOFError:
        user_input = 'n'

    if user_input != 'y':
        print("Renaming cancelled.")
        return False

    print("Renaming files...")
    count = 0
    for f in files_to_rename:
        new_number_str = f['number_str'].zfill(padding_width)
        new_filename = f"{f['prefix']}{new_number_str}{f['suffix']}"
        old_path = f['path']
        new_path = os.path.join(f['dir'], new_filename)
        try:
            os.rename(old_path, new_path)
            count += 1
        except OSError:
            pass
    print(f"Successfully renamed {count} files.")
    return True


def main():
    root = Tk()
    root.withdraw()
    selected_folder = filedialog.askdirectory(title="Select the ROOT folder of your experiment")

    if not selected_folder:
        print("No folder selected. Exiting.")
        return

    print(f"Selected data folder: {selected_folder}")

    cbf_file = find_file_by_suffix(selected_folder, CBF_METADATA_FILENAME)
    raw_data_folder = os.path.join(selected_folder, "images", "RAW_DATA")
    ome_file = os.path.join(raw_data_folder, OME_METADATA_FILENAME)
    
    check_and_rename_files(raw_data_folder)

    if not all([cbf_file, os.path.isdir(raw_data_folder), os.path.exists(ome_file)]):
        print("Error: Could not find required files/folders.")
        return

    params = parse_metadata(cbf_file, ome_file)
    if not params: return

    available_channels = get_available_channels(raw_data_folder, params)
    print(f"\n--- Detected Channels: {available_channels} ---")
    
    selected_channel = None
    if len(available_channels) == 1:
        selected_channel = available_channels[0]
        print(f"Only one channel detected ({selected_channel}). Selecting automatically.")
    else:
        while True:
            try:
                user_input = input(f"Enter the channel number to stitch {available_channels}: ")
                selection = int(user_input)
                if selection in available_channels:
                    selected_channel = selection
                    break
                else:
                    print(f"Invalid selection. Please choose from {available_channels}")
            except ValueError:
                print("Invalid input. Please enter a number.")
    
    print(f"\n--- Processing Channel {selected_channel} ---")
    slice_count = verify_slice_counts(raw_data_folder, params, selected_channel)
    if slice_count > 0:
        xml_output_filename = f"terastitcher_import_c{selected_channel}.xml"
        xml_output_path = os.path.join(raw_data_folder, xml_output_filename)
        
        generate_terastitcher_xml(xml_output_path, params, raw_data_folder, selected_channel, slice_count)
        generate_execution_script(raw_data_folder, xml_output_filename, selected_channel)
        
        print("\nProcess finished successfully!")
        if sys.platform.startswith('win'):
            print(f"Run 'run_stitching_c{selected_channel}.bat' to stitch.")
        else:
            print(f"Run './run_stitching_c{selected_channel}.sh' to stitch.")

if __name__ == '__main__':
    main()