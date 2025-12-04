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
    Calculates the physical step size between tiles in PIXELS based on stage coordinates.
    Assumes stage units are in Nanometers (1e-3 microns), common for Inscoper.
    """
    print("\n--- Calculating Precise Steps from Stage Coordinates ---")
    
    try:
        mosaic = cbf_data['mda']['list'][0]['mosaic']['MosaicElems'][0]
        positions_list = mosaic['Positions']
        
        # We need at least 2 points to calculate a step
        if len(positions_list) < 2:
            print("  - Warning: Not enough positions to calculate overlap. Defaulting to 10% overlap.")
            return 2048 * 0.9, 2048 * 0.9, 1, 1

        # 1. Extract X and Y stage coordinates for all positions
        coords = []
        for pos in positions_list:
            x = 0.0
            y = 0.0
            for device in pos['devices']:
                # Axis1 is usually Stage X, Axis2 is usually Stage Y
                if "Axis1AbsolutePosition" in device['Id'].get('SubDeviceId', ''):
                    x = float(device['Value'])
                elif "Axis2AbsolutePosition" in device['Id'].get('SubDeviceId', ''):
                    y = float(device['Value'])
            coords.append((x, y))

        # 2. Determine Grid Layout and Axis Mapping
        # User Requirement: Index 0 is Top-Left. Index 1 is to the Right (Horizontal Neighbor).
        # We check coordinate change between Index 0 and Index 1 to see which Stage Axis corresponds to Image Horizontal.
        
        p0 = coords[0]
        p1 = coords[1]
        
        dx_01 = abs(p1[0] - p0[0])
        dy_01 = abs(p1[1] - p0[1])
        
        # Heuristic: The axis with the larger change between idx 0 and 1 is the Horizontal Axis.
        if dx_01 > dy_01:
            print("  - Detected: Stage X corresponds to Image Horizontal (Row-Major filling).")
            # H step is driven by X, V step is driven by Y
            h_stage_vals = [c[0] for c in coords]
            v_stage_vals = [c[1] for c in coords]
        else:
            print("  - Detected: Stage Y corresponds to Image Horizontal (Row-Major filling).")
            # H step is driven by Y, V step is driven by X
            h_stage_vals = [c[1] for c in coords]
            v_stage_vals = [c[0] for c in coords]

        # 3. Calculate Average Steps (Delta) in Stage Units
        # We find unique values to determine grid dimensions and average step
        
        # Rounding to nearest 100 units to group slightly jittered positions
        unique_h = sorted(list(set([round(v, -2) for v in h_stage_vals])))
        unique_v = sorted(list(set([round(v, -2) for v in v_stage_vals])))
        
        grid_width = len(unique_h) # Number of columns
        grid_height = len(unique_v) # Number of rows
        
        print(f"  - Grid Dimensions Detected: {grid_width} Cols x {grid_height} Rows")

        # Calculate H Step (Horizontal)
        if grid_width > 1:
            steps_h = [abs(unique_h[i+1] - unique_h[i]) for i in range(len(unique_h)-1)]
            avg_step_stage_h = sum(steps_h) / len(steps_h)
        else:
            avg_step_stage_h = 0
            
        # Calculate V Step (Vertical)
        if grid_height > 1:
            steps_v = [abs(unique_v[i+1] - unique_v[i]) for i in range(len(unique_v)-1)]
            avg_step_stage_v = sum(steps_v) / len(steps_v)
        else:
            avg_step_stage_v = 0
            
        # 4. Convert Stage Units to Pixels
        # Assumption: Stage units are Nanometers (1e-3 microns). 
        # Verification: Typical step is ~2.6e6. 2.6e6 * 1e-3 = 2660um. FOV is ~2900um. Matches.
        
        step_microns_h = avg_step_stage_h * 1e-3
        step_microns_v = avg_step_stage_v * 1e-3
        
        step_pixels_h = step_microns_h / voxel_size_h
        step_pixels_v = step_microns_v / voxel_size_v
        
        # Fallback if single row/col
        if step_pixels_h == 0: step_pixels_h = 2048 * 0.9 # Default 10% overlap
        if step_pixels_v == 0: step_pixels_v = 2048 * 0.9

        print(f"  - Calculated Step H: {step_pixels_h:.2f} pixels ({step_microns_h:.2f} um)")
        print(f"  - Calculated Step V: {step_pixels_v:.2f} pixels ({step_microns_v:.2f} um)")
        
        return step_pixels_h, step_pixels_v, grid_width, grid_height

    except Exception as e:
        print(f"  - Error during coordinate calculation: {e}")
        print("  - Fallback: Using default theoretical overlap (10%).")
        return 2048*0.9, 2048*0.9, 5, 5 # Fallback defaults


def parse_metadata(cbf_path, ome_path):
    """Parses metadata files to extract experiment parameters."""
    params = {}
    print("Parsing metadata...")
    try:
        with open(cbf_path, 'r', encoding='utf-8') as f:
            cbf_data = json.load(f)
        
        # Get Image Dimensions (assume 2048 if missing, as legacy 240 is definitely wrong)
        # Note: 'IMAGE WIDTH' is inside the 'Settings' list or 'PresetChannel', searching recursively is safer,
        # but for this specific file structure we check the first available camera setting.
        try:
             # Basic fallback
            img_width = 2048 
            img_height = 2048
            # Try to find in cameras
            for cam in cbf_data.get('ROIs', {}).get('PresetChannel', []):
                 for dev in cam.get('devices', []):
                     if dev.get('Id', {}).get('SubDeviceId') == 'IMAGE WIDTH':
                         img_width = int(dev['Value'])
                     if dev.get('Id', {}).get('SubDeviceId') == 'IMAGE HEIGHT':
                         img_height = int(dev['Value'])
        except:
            img_width = 2048
            img_height = 2048

        params['img_width_px'] = img_width
        params['img_height_px'] = img_height
        
        # Calculate Steps in Pixels
        step_h_px, step_v_px, grid_w, grid_h = calculate_steps_from_coordinates(
            cbf_data, VOXEL_SIZE_OVERRIDE["H"], VOXEL_SIZE_OVERRIDE["V"]
        )

        params['step_H_px'] = floor(step_h_px)
        params['step_V_px'] = floor(step_v_px)
        params['gridH'] = grid_w
        params['gridV'] = grid_h

        # Calculate estimated overlap for report
        ov_h = 1.0 - (step_h_px / img_width)
        print(f"  - Effective Overlap H: {ov_h*100:.2f}%")
        
    except Exception as e:
        print(f"Error parsing CBF file '{cbf_path}': {e}")
        return None

    try:
        tree = ET.parse(ome_path)
        root = tree.getroot()
        ns = {'ome': 'http://www.openmicroscopy.org/Schemas/OME/2016-06'}
        pixels_node = root.find('ome:Image/ome:Pixels', ns)
        if pixels_node is None: raise ValueError("Pixels node not found in OME-XML.")
        
        params.update({
            'no_slices_metadata': int(pixels_node.get('SizeZ'))
        })
    except Exception as e:
        print(f"Error parsing OME-XML file '{ome_path}': {e}")
        return None

    params['voxel_H'] = VOXEL_SIZE_OVERRIDE["H"]
    params['voxel_V'] = VOXEL_SIZE_OVERRIDE["V"]
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

    # --- CORRECTION START ---
    # Mechanical displacements in the XML Header must be in MICRONS (Physical Units)
    # because voxel_dims are defined in microns.
    # We multiply the pixel step by the voxel size.
    mech_disp_H_microns = params['step_H_px'] * params['voxel_H']
    mech_disp_V_microns = params['step_V_px'] * params['voxel_V']
    # --- CORRECTION END ---
    
    # Dimensions in microns
    vxl_V = params['voxel_H']
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
    
    # --- UPDATED LINE BELOW ---
    ET.SubElement(terastitcher_node, 'mechanical_displacements', V=str(mech_disp_V_microns), H=str(mech_disp_H_microns))
    
    ET.SubElement(terastitcher_node, 'dimensions',
               stack_rows=str(params['gridV']),
               stack_columns=str(params['gridH']),
               stack_slices=str(slice_count))

    stacks_node = ET.SubElement(terastitcher_node, 'STACKS')
    
    for row in range(params['gridV']):
        for col in range(params['gridH']):
            
            # NOTE: ABS_V and ABS_H inside the Stack elements MUST remain in PIXELS.
            # TeraStitcher expects raw coordinates here.
            abs_H_eff = col * params['step_H_px']
            abs_V_eff = row * params['step_V_px']
            
            folder_index = row * params['gridH'] + col
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