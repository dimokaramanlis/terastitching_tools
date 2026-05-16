#!/usr/bin/env python
"""
This script automatically generates a TeraStitcher XML descriptor and execution
script for MULTI-CHANNEL datasets using exact stage coordinates from CBF metadata.

Supports TWO folder structures:
  A) Single experiment:  root/ has one .cbf and images/RAW_DATA/ with channels c0, c1, ...
  B) Split-channel:      root/ has sub-folders, each with its own .cbf and images/RAW_DATA/
     Each sub-folder is ONE channel (files always use c0).  User picks the reference
     folder for alignment; the computed stitching is applied to every other folder.

Author: DK & Gemini
Date: January 08, 2026 -- Extended May 2026
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

def extract_positions_from_cbf(cbf_data, params):
    """Extracts exact absolute positions from the CBF JSON structure."""
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
            # Mapping Axis2 to X/H and Axis1 to Y/V based on the stage configuration
            if dev_id == 'Axis2AbsolutePosition':
                val_x = dev.get('Value', 0)
            elif dev_id == 'Axis1AbsolutePosition':
                val_y = dev.get('Value', 0)
        coords.append((val_x, val_y))

    # To match image coordinate space (top-left is 0,0; increasing down/right),
    # we invert the microscope stage coordinates using the max value as the origin.
    max_x = max(c[0] for c in coords)
    max_y = max(c[1] for c in coords)
    
    # Robust fallback for nominal step size mapping
    step_x_nm = mosaic.get('UserOffsetX')
    if not step_x_nm:
        unique_x = sorted(list(set(c[0] for c in coords)))
        step_x_nm = unique_x[1] - unique_x[0] if len(unique_x) > 1 else 1

    step_y_nm = mosaic.get('UserOffsetY')
    if not step_y_nm:
        unique_y = sorted(list(set(c[1] for c in coords)))
        step_y_nm = unique_y[1] - unique_y[0] if len(unique_y) > 1 else 1

    tile_data = []
    for i, (cx, cy) in enumerate(coords):
        # Calculate Row and Col mapping
        col = int(round((max_x - cx) / step_x_nm))
        row = int(round((max_y - cy) / step_y_nm))
        
        # Calculate ABS_H and ABS_V strictly from physical nm converted to pixels
        abs_h = (max_x - cx) * 1e-3 / params['voxel_H']
        abs_v = (max_y - cy) * 1e-3 / params['voxel_V']
        
        tile_data.append({
            'index': i,
            'row': row,
            'col': col,
            'abs_h': int(round(abs_h)),
            'abs_v': int(round(abs_v))
        })

    grid_w = mosaic.get('xTileNumber', max(t['col'] for t in tile_data) + 1)
    grid_h = mosaic.get('yTileNumber', max(t['row'] for t in tile_data) + 1)

    mech_disp_H_microns = step_x_nm * 1e-3
    mech_disp_V_microns = step_y_nm * 1e-3

    return tile_data, grid_w, grid_h, mech_disp_H_microns, mech_disp_V_microns


def parse_metadata(cbf_path, ome_path):
    params = {}
    print("Parsing metadata...")
    
    # 1. Get dimensions from OME, but rigidly lock XY voxel sizes to fallback
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

        # FORCE the empirically true 1.44 value for XY to avoid offset inflation from bad OME data
        params['voxel_H'] = VOXEL_SIZE_FALLBACK["H"]
        params['voxel_V'] = VOXEL_SIZE_FALLBACK["V"]
        params['z_spacing'] = get_dim('PhysicalSizeZ', VOXEL_SIZE_FALLBACK["D"])
        params['no_slices_metadata'] = int(pixels_node.get('SizeZ'))
        params['img_width_px'] = int(pixels_node.get('SizeX'))
        params['img_height_px'] = int(pixels_node.get('SizeY'))
    except Exception as e:
        print(f"Error parsing OME: {e}")
        return None

    # 2. Extract exact coordinates using CBF
    try:
        with open(cbf_path, 'r', encoding='utf-8') as f:
            cbf_data = json.load(f)
            
        extracted = extract_positions_from_cbf(cbf_data, params)
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
    

def get_available_channels(data_folder, params):
    """Scans the folder to detect which channels (c0, c1...) are present."""
    channels = set()
    pattern = re.compile(r"_c(\d+)_z")
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

    vxl_V = params['voxel_V']
    vxl_H = params['voxel_H']
    vxl_D = params['z_spacing']

    channel_regex = f"_c{channel_idx}_z.*\\.ome\\.tif"

    terastitcher_node = ET.Element('TeraStitcher')
    terastitcher_node.set('volume_format', 'TiledXY|2Dseries')
    terastitcher_node.set('input_plugin', 'tiff2D')

    ET.SubElement(terastitcher_node, 'stacks_dir', value=str(data_folder))
    # NOTE: Each channel MUST have its own metadata bin file
    ET.SubElement(terastitcher_node, 'mdata_bin', value=os.path.join(data_folder, f'mdata_c{channel_idx}.bin'))
    ET.SubElement(terastitcher_node, 'ref_sys', ref1="1", ref2="2", ref3="3")
    ET.SubElement(terastitcher_node, 'voxel_dims', V=str(vxl_V), H=str(vxl_H), D=str(vxl_D))
    ET.SubElement(terastitcher_node, 'origin', V="0", H="0", D="0")
    ET.SubElement(terastitcher_node, 'mechanical_displacements', V=str(params['mech_disp_V_microns']), H=str(params['mech_disp_H_microns']))
    
    ET.SubElement(terastitcher_node, 'dimensions',
               stack_rows=str(params['gridV']),
               stack_columns=str(params['gridH']),
               stack_slices=str(slice_count))

    stacks_node = ET.SubElement(terastitcher_node, 'STACKS')
    
    # Sort tiles to ensure TeraStitcher establishes the correct global bounding box
    sorted_tiles = sorted(params['tile_data'], key=lambda t: (t['row'], t['col']))
    
    for tile in sorted_tiles:
        dir_name = TILE_FOLDER_FORMAT.format(index=tile['index'])
        
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

def create_propagation_script(output_folder, main_channel_idx, available_channels):
    """
    Creates a Python script that CLONES the aligned reference XML and 
    PATCHES it for all other channels (updating mdata path and regex).
    This ensures all channels share the exact same grid topology.
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
    # This is the file produced by the alignment steps on the MAIN channel
    aligned_xml = f"xml_merging_c{{MAIN_CHANNEL}}.xml"
    
    if not os.path.exists(aligned_xml):
        print(f"Error: Aligned file {{aligned_xml}} not found! Alignment failed?")
        sys.exit(1)
        
    print(f"Cloning alignment from reference: {{aligned_xml}}")
    
    # Iterate over all OTHER channels
    for c_idx in CHANNELS:
        if c_idx == MAIN_CHANNEL:
            continue
            
        target_xml = f"xml_merging_c{{c_idx}}.xml"
        
        try:
            tree = ET.parse(aligned_xml)
            root = tree.getroot()
            
            # 1. Update mdata_bin path
            # Look for <mdata_bin value="...">
            mdata_node = root.find("mdata_bin")
            if mdata_node is not None:
                old_val = mdata_node.get("value")
                new_val = old_val.replace(f"mdata_c{{MAIN_CHANNEL}}.bin", f"mdata_c{{c_idx}}.bin")
                mdata_node.set("value", new_val)
                
            # 2. Update IMG_REGEX in every Stack
            # Look for IMG_REGEX="_c0_z" -> replace with "_c1_z"
            old_regex_part = f"_c{{MAIN_CHANNEL}}_z"
            new_regex_part = f"_c{{c_idx}}_z"
            
            stacks = root.findall(".//Stack")
            print(f"  -> Generating {{target_xml}} (Updating {{len(stacks)}} stacks)...")
            
            for stack in stacks:
                regex = stack.get("IMG_REGEX")
                if regex and old_regex_part in regex:
                    stack.set("IMG_REGEX", regex.replace(old_regex_part, new_regex_part))
                
                # Also ensure STITCHABLE is 'no' (it should be 'no' in merging XML anyway, but good safety)
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
    with open(script_path, 'w', encoding='utf-8') as f:
        f.write(content)
        
def generate_execution_script(output_folder, available_channels, main_channel_idx):
    """Generates the batch script following the official MultiChannel alignment workflow."""
    is_windows = sys.platform.startswith('win')
    script_filename = f"run_stitching_multi.{'bat' if is_windows else 'sh'}"
    script_path = os.path.join(output_folder, script_filename)
    
    main_import_xml = f"terastitcher_import_c{main_channel_idx}.xml"
    main_merging_xml = f"xml_merging_c{main_channel_idx}.xml"
    
    # Params from Global Dict
    imout_depth = DEFAULT_STITCHING_PARAMS["imout_depth"]
    subvoldim = DEFAULT_STITCHING_PARAMS["subvoldim"]
    sV = DEFAULT_STITCHING_PARAMS["sV"]
    sH = DEFAULT_STITCHING_PARAMS["sH"]
    sD = DEFAULT_STITCHING_PARAMS["sD"]
    thres = DEFAULT_STITCHING_PARAMS["displ_threshold"]
    
    lines = []
    
    lines.append("@echo off")
    lines.append(f"echo --- Phase 1: Generating Test Projections for ALL channels ---")
    
    # 1. Test Projections Loop
    for c in available_channels:
        lines.append(f"echo Generating test image for Channel {c}...")
        # Run test generation
        lines.append(f'terastitcher --test --projin="terastitcher_import_c{c}.xml" --imout_depth={imout_depth} --sparse_data')
        
        # Rename the output to avoid overwriting. 
        # TeraStitcher default output for --test is usually 'test_middle_slice.tif'
        lines.append(f'if exist "test_middle_slice.tif" ren "test_middle_slice.tif" "test_c{c}.tif"')
    
    # 2. User Confirmation
    lines.append(f"echo.")
    lines.append(f"echo ----------------------------------------------------------------")
    lines.append(f"echo Please check the generated 'test_cX.tif' images in the folder.")
    lines.append(f"echo ----------------------------------------------------------------")
    lines.append('SET /P continue="Are the projections correct (y/n)? "')
    lines.append('IF /I "%continue%" NEQ "y" EXIT /B 1')
    
    # 3. Alignment (Main Channel Only)
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

    # 4. Propagation
    lines.append("\n" + f"echo --- Phase 3: Propagating Alignment to Satellite Channels ---")
    lines.append(f'{sys.executable} propagate_xmls.py')
    lines.append("IF %ERRORLEVEL% NEQ 0 GOTO ERROR")

    # 5. Independent Merge
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
         
def check_and_rename_files(data_folder):
    """Renames files to ensure zero-padding."""
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

    if not files_to_process: return True

    padding_width = max(4, len(str(max_z_number)))
    files_to_rename = [f for f in files_to_process if len(f['number_str']) < padding_width]

    if not files_to_rename: return True

    print(f"\nWARNING: WARNING: Found {len(files_to_rename)} files to rename (Padding to {padding_width} digits).")
    try:
        user_input = input("Proceed with renaming? (y/n): ").lower()
    except EOFError:
        user_input = 'n'

    if user_input != 'y': return False

    print("Renaming files...")
    for f in files_to_rename:
        new_number_str = f['number_str'].zfill(padding_width)
        new_filename = f"{f['prefix']}{new_number_str}{f['suffix']}"
        try:
            os.rename(f['path'], os.path.join(f['dir'], new_filename))
        except OSError:
            pass
    return True


# ==========================================================================
#  MULTI-FOLDER SUPPORT  (each sub-folder = one channel, all files use c0)
# ==========================================================================

def discover_experiments(root_folder):
    """
    Detects folder structure.

    Returns a list of experiment dicts:
        cbf_path, raw_data_folder, ome_path, subfolder_name (None -> single)
    """
    # Case A: root itself is the experiment
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

    # Case B: sub-folders are separate channels
    experiments = []
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


def create_propagation_script_multi(output_folder, ref_name, folder_names, raw_data_paths):
    """
    Multi-folder propagation: clones the aligned reference XML and patches
    stacks_dir + mdata_bin to point at each satellite folder's RAW_DATA.
    IMG_REGEX is left untouched (all folders use c0).
    DIR_NAME is left untouched (all folders use image_xy0, image_xy1, ...).
    """
    script_path = os.path.join(output_folder, "propagate_xmls.py")

    # Build a Python dict literal mapping folder name -> RAW_DATA absolute path
    path_map_str = "{\n"
    for name in folder_names:
        path_map_str += f"    {repr(name)}: {repr(raw_data_paths[name])},\n"
    path_map_str += "}"

    content = f"""
import xml.etree.ElementTree as ET
import os
import sys

# AUTO-GENERATED SCRIPT to propagate TeraStitcher alignments (multi-folder mode)
# Each folder is one channel -- all use c0 internally.  We only swap paths.
REF_FOLDER = {repr(ref_name)}
FOLDER_NAMES = {repr(folder_names)}
RAW_DATA_PATHS = {path_map_str}

def main():
    # Ensure we run from the script's own directory
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
    print("--- Running XML Propagation Step (multi-folder) ---")
    aligned_xml = f"xml_merging_{{REF_FOLDER}}.xml"

    if not os.path.exists(aligned_xml):
        print(f"Error: Aligned file {{aligned_xml}} not found! Alignment failed?")
        sys.exit(1)

    print(f"Cloning alignment from reference: {{aligned_xml}}")

    ref_raw = RAW_DATA_PATHS[REF_FOLDER]

    for folder_name in FOLDER_NAMES:
        if folder_name == REF_FOLDER:
            continue

        target_xml = f"xml_merging_{{folder_name}}.xml"
        sat_raw = RAW_DATA_PATHS[folder_name]

        try:
            tree = ET.parse(aligned_xml)
            root = tree.getroot()

            # 1. Swap stacks_dir -> satellite folder's RAW_DATA
            stacks_node = root.find("stacks_dir")
            if stacks_node is not None:
                stacks_node.set("value", sat_raw)

            # 2. Swap mdata_bin path
            mdata_node = root.find("mdata_bin")
            if mdata_node is not None:
                old_val = mdata_node.get("value")
                new_val = old_val.replace(ref_raw, sat_raw)
                mdata_node.set("value", new_val)

            stacks = root.findall(".//Stack")
            print(f"  -> Generating {{target_xml}} ({{len(stacks)}} stacks)...")

            for stack in stacks:
                stack.set("STITCHABLE", "no")

            tree.write(target_xml)
            print(f"     [OK] Saved {{target_xml}}")

        except Exception as e:
            print(f"Error processing {{folder_name}}: {{e}}")
            sys.exit(1)

    print("Propagation complete.")

if __name__ == "__main__":
    main()
"""
    with open(script_path, 'w', encoding='utf-8') as f:
        f.write(content)


def generate_execution_script_multi(output_folder, folder_names, ref_name):
    """
    Generates the batch/shell script for the multi-folder workflow.
    Alignment runs on the reference folder only, then is propagated to all
    satellite folders before each folder is merged independently.
    """
    is_windows = sys.platform.startswith('win')
    script_filename = f"run_stitching_multi.{'bat' if is_windows else 'sh'}"
    script_path = os.path.join(output_folder, script_filename)

    ref_import_xml = f"terastitcher_import_{ref_name}.xml"
    ref_merging_xml = f"xml_merging_{ref_name}.xml"

    imout_depth = DEFAULT_STITCHING_PARAMS["imout_depth"]
    subvoldim = DEFAULT_STITCHING_PARAMS["subvoldim"]
    sV = DEFAULT_STITCHING_PARAMS["sV"]
    sH = DEFAULT_STITCHING_PARAMS["sH"]
    sD = DEFAULT_STITCHING_PARAMS["sD"]
    thres = DEFAULT_STITCHING_PARAMS["displ_threshold"]

    lines = []

    lines.append("@echo off")
    lines.append('cd /d "%~dp0"')
    lines.append("echo --- Phase 1: Generating Test Projections for ALL folders ---")

    # Use %~dp0 (bat file's directory with trailing backslash) to build absolute
    # paths for every XML.  TeraStitcher may write --projout relative to stacks_dir
    # instead of CWD, so relative filenames break in multi-folder mode.
    BD = "%~dp0"   # batch-dir placeholder used in the generated strings

    # 1. Test projections for every folder
    for name in folder_names:
        lines.append(f"echo Generating test image for [{name}]...")
        lines.append(f'terastitcher --test --projin="{BD}terastitcher_import_{name}.xml" --imout_depth={imout_depth} --sparse_data')
        lines.append(f'if exist "test_middle_slice.tif" ren "test_middle_slice.tif" "{BD}test_{name}.tif"')

    # 2. User confirmation
    lines.append("echo.")
    lines.append("echo ----------------------------------------------------------------")
    lines.append("echo Please check the generated 'test_*.tif' images in the folder.")
    lines.append("echo ----------------------------------------------------------------")
    lines.append('SET /P continue="Are the projections correct (y/n)? "')
    lines.append('IF /I "%continue%" NEQ "y" EXIT /B 1')

    # 3. Alignment on reference folder only
    lines.append("echo.")
    lines.append(f"echo --- Phase 2: Aligning Reference Folder [{ref_name}] ---")
    lines.append(f'terastitcher --displcompute --projin="{BD}{ref_import_xml}" --projout="{BD}xml_comp.xml" --subvoldim={subvoldim} --sV={sV} --sH={sH} --sD={sD} --sparse_data')
    lines.append("IF %ERRORLEVEL% NEQ 0 GOTO ERROR")

    lines.append(f'terastitcher --displproj --projin="{BD}xml_comp.xml" --projout="{BD}xml_proj.xml" --sparse_data')
    lines.append("IF %ERRORLEVEL% NEQ 0 GOTO ERROR")

    lines.append(f'terastitcher --displthres --projin="{BD}xml_proj.xml" --projout="{BD}xml_thres.xml" --threshold={thres} --sparse_data')
    lines.append("IF %ERRORLEVEL% NEQ 0 GOTO ERROR")

    lines.append(f'terastitcher --placetiles --projin="{BD}xml_thres.xml" --projout="{BD}{ref_merging_xml}" --sparse_data')
    lines.append("IF %ERRORLEVEL% NEQ 0 GOTO ERROR")

    # 4. Propagation to satellite folders
    lines.append("\n" + "echo --- Phase 3: Propagating Alignment to Satellite Folders ---")
    lines.append(f'{sys.executable} "{BD}propagate_xmls.py"')
    lines.append("IF %ERRORLEVEL% NEQ 0 GOTO ERROR")

    # 5. Independent merge per folder
    lines.append("\n" + "echo --- Phase 4: Merging All Folders Independently ---")
    for name in folder_names:
        input_xml = f"xml_merging_{name}.xml"
        output_dir = f"Stitched_{name}"
        lines.append(f'echo Merging [{name}]...')
        lines.append(f'if not exist "{BD}{output_dir}" mkdir "{BD}{output_dir}"')
        lines.append(f'terastitcher --merge --projin="{BD}{input_xml}" --volout="{BD}{output_dir}" --resolutions=024 --imout_depth={imout_depth} --sparse_data')
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


def run_multi_experiment(selected_folder, experiments):
    """
    Orchestrates the multi-folder workflow where each sub-folder contains
    one channel (all files use c0).  The user picks which folder to use for
    computing the alignment; the result is propagated to all other folders.
    """
    folder_names = [e['subfolder_name'] for e in experiments]
    exp_by_name = {e['subfolder_name']: e for e in experiments}

    # --- Rename check across all RAW_DATA folders ---
    for exp in experiments:
        check_and_rename_files(exp['raw_data_folder'])

    # --- Parse metadata for each folder independently ---
    all_params = {}
    for exp in experiments:
        name = exp['subfolder_name']
        print(f"\n=== Parsing [{name}] ===")
        params = parse_metadata(exp['cbf_path'], exp['ome_path'])
        if not params:
            print(f"Error: Could not parse metadata for {name}")
            return
        all_params[name] = params

    # --- Show channel info for each folder to help user choose ---
    print("\n--- Detected Folders (each = one channel) ---")
    for i, name in enumerate(folder_names):
        descs = get_channel_descriptions(exp_by_name[name]['ome_path'])
        ch_info = descs.get('0', "No OME channel info")
        print(f"  [{i}] {name}:  {ch_info}")

    # --- Ask user which folder to use for alignment ---
    ref_name = None
    if len(experiments) == 1:
        ref_name = folder_names[0]
        print(f"\nOnly one folder detected ({ref_name}). Selecting automatically.")
    else:
        print()
        while True:
            try:
                user_input = input(
                    f"Enter the folder to use for ALIGNMENT (name or index) {folder_names}: "
                ).strip()
                # Accept by exact name
                if user_input in folder_names:
                    ref_name = user_input
                    break
                # Accept by numeric index
                idx = int(user_input)
                if 0 <= idx < len(folder_names):
                    ref_name = folder_names[idx]
                    break
                print("Invalid selection.")
            except (ValueError, IndexError):
                print("Invalid selection. Enter the folder name or its index.")

    print(f"\n--- Reference folder for alignment: [{ref_name}] ---")
    print(f"--- Generating Files ---\n")

    # Output folder = root (all XMLs and scripts live here, next to the sub-folders)
    output_folder = selected_folder

    # --- Generate import XML per folder (channel is always c0) ---
    for name in folder_names:
        exp = exp_by_name[name]
        params = all_params[name]
        slice_count = verify_slice_counts(exp['raw_data_folder'], params, 0)
        xml_path = os.path.join(output_folder, f"terastitcher_import_{name}.xml")
        generate_terastitcher_xml(xml_path, params, exp['raw_data_folder'], 0, slice_count)

    # --- Generate propagation script (swaps stacks_dir + mdata_bin) ---
    raw_data_paths = {name: exp_by_name[name]['raw_data_folder'] for name in folder_names}
    create_propagation_script_multi(output_folder, ref_name, folder_names, raw_data_paths)

    # --- Generate execution script ---
    generate_execution_script_multi(output_folder, folder_names, ref_name)

    print("\nProcess finished successfully!")
    if sys.platform.startswith('win'):
        print(f"Run 'run_stitching_multi.bat' inside: {selected_folder}")
    else:
        print(f"Run './run_stitching_multi.sh' inside: {selected_folder}")


# ==========================================================================
#  MAIN -- auto-detects single vs multi and dispatches accordingly
# ==========================================================================

def main():
    root = Tk()
    root.withdraw()
    selected_folder = filedialog.askdirectory(title="Select the ROOT folder of your experiment")

    if not selected_folder: return

    print(f"Selected data folder: {selected_folder}")

    # --- Discover structure ---
    experiments = discover_experiments(selected_folder)

    if not experiments:
        print("Error: Could not find any valid experiment data.")
        print("Expected either:")
        print("  A) A .cbf file at root level  +  images/RAW_DATA/  subfolder")
        print("  B) Sub-folders, each with a .cbf  +  images/RAW_DATA/")
        return

    is_single = (len(experiments) == 1 and experiments[0]['subfolder_name'] is None)

    if is_single:
        # ============================================================
        #  ORIGINAL SINGLE-FOLDER PATH  (completely unchanged)
        # ============================================================
        print("Mode: SINGLE experiment folder\n")

        cbf_file        = experiments[0]['cbf_path']
        raw_data_folder = experiments[0]['raw_data_folder']
        ome_file        = experiments[0]['ome_path']
        channel_details = get_channel_descriptions(ome_file)

        check_and_rename_files(raw_data_folder)

        if not all([cbf_file, os.path.isdir(raw_data_folder), os.path.exists(ome_file)]):
            print("Error: Could not find required files/folders.")
            return

        params = parse_metadata(cbf_file, ome_file)
        if not params: return

        available_channels = get_available_channels(raw_data_folder, params)
        print("\n--- Available Channels Detected ---")
        for c_id in sorted(available_channels):
            info = channel_details.get(str(c_id), "No metadata found")
            print(f"[{c_id}]: {info}")

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

        for c_idx in available_channels:
            slice_count = verify_slice_counts(raw_data_folder, params, c_idx)
            xml_path = os.path.join(raw_data_folder, f"terastitcher_import_c{c_idx}.xml")
            generate_terastitcher_xml(xml_path, params, raw_data_folder, c_idx, slice_count)

        create_propagation_script(raw_data_folder, selected_channel, available_channels)

        generate_execution_script(
            raw_data_folder,
            available_channels,
            selected_channel)

        print("\nProcess finished successfully!")
        if sys.platform.startswith('win'):
            print(f"Run 'run_stitching_multi.bat' inside the RAW_DATA folder.")
        else:
            print(f"Run './run_stitching_multi.sh' inside the RAW_DATA folder.")

    else:
        # ============================================================
        #  MULTI-FOLDER PATH  (each folder = one channel, all use c0)
        # ============================================================
        names = [e['subfolder_name'] for e in experiments]
        print(f"Mode: MULTI-FOLDER  ({len(experiments)} folders: {names})\n")
        run_multi_experiment(selected_folder, experiments)


if __name__ == '__main__':
    main()
