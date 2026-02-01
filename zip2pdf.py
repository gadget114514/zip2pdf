import zipfile
import pdfkit
import argparse
import os
import sys
import io
import base64
import mimetypes
import re
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
import time

# Global control for interruptions
stop_event = threading.Event()
active_procs = []
procs_lock = threading.Lock()

def get_b64_src(zip_map, current_html_path, linked_path, depth, verbose, css_cache=None, css_lock=None):
    if stop_event.is_set(): return None
    if depth > 5:
        if verbose: print(f"    [SKIP] Depth limit reached for {linked_path}")
        return None
    
    # Normalize the linked path for searching
    clean_path = linked_path.replace('\\', '/').strip()
    
    # Helper to find data in zip_map
    def find_in_zip(target):
        target = target.replace('\\', '/').strip('/')
        return zip_map.get(target)

    data = None
    found_path = clean_path
    
    # Strategy 1: Absolute or direct path check
    data = find_in_zip(clean_path)
    
    # Strategy 2: Relative to current HTML location
    if data is None and current_html_path:
        html_dir = os.path.dirname(current_html_path)
        rel_candidate = os.path.normpath(os.path.join(html_dir, clean_path)).replace('\\', '/')
        data = find_in_zip(rel_candidate)
        if data: found_path = rel_candidate

    # Strategy 3: Global filename search (Last resort)
    if data is None:
        basename = os.path.basename(clean_path)
        for z_path, z_data in zip_map.items():
            if os.path.basename(z_path) == basename:
                data = z_data
                found_path = z_path
                break
    
    if data:
        if verbose: print(f"    [RESOLVED] {linked_path}")
        ext = os.path.splitext(found_path.lower())[1]
        mime = mimetypes.guess_type(found_path)[0] or 'application/octet-stream'
        
        # If it's a CSS file, we should recursively resolve its imports (depth + 1)
        if ext == '.css':
            # Check cache first
            if css_cache is not None and found_path in css_cache:
                if verbose and depth <= 1: print(f"    [CACHE] Hit for {found_path}")
                data = css_cache[found_path]
            else:
                try:
                    css_text = data.decode('utf-8')
                except UnicodeDecodeError:
                    css_text = data.decode('latin-1')
                
                # Resolve recursively
                resolved_css = resolve_resources(zip_map, found_path, css_text, depth + 1, verbose, css_cache, css_lock).encode('utf-8')
                data = resolved_css
                
                # Update cache safely
                if css_cache is not None and css_lock is not None:
                    with css_lock:
                        css_cache[found_path] = data

        b64 = base64.b64encode(data).decode('utf-8')
        return f"data:{mime};base64,{b64}"
    elif verbose and not linked_path.startswith(('http', 'data')):
        print(f"    [MISSING]  {linked_path}")
    return None

def resolve_resources(zip_map, current_file, content, depth, verbose, css_cache=None, css_lock=None, ignore_icons=False):
    if depth > 5: return content
    
    if verbose: print(f"  > [{depth}] Resolving resources for {current_file}...")
    
    def apply_resolution(text, is_head=False):
        def replacer(match):
            attr = match.group(1)
            path = match.group(2)
            if path.lower().startswith(('http://', 'https://', 'data:')):
                return match.group(0)
            
            if ignore_icons:
                ext = os.path.splitext(path.lower())[1]
                is_favicon = "favicon" in path.lower() or ext == ".ico"
                
                if is_favicon or (is_head and ext in {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'}):
                    if verbose and is_favicon: print(f"    [SKIP] Ignoring icon/favicon: {path}")
                    return match.group(0)
            
            b64 = get_b64_src(zip_map, current_file, path, depth, verbose, css_cache, css_lock)
            return f'{attr}="{b64}"' if b64 else match.group(0)
        
        text = re.sub(r'(src|href)=["\'](.*?)["\']', replacer, text)
        
        def css_url_repl(match):
            path = match.group(1).strip('"\'')
            if path.lower().startswith(('http', 'data')): return match.group(0)
            b64 = get_b64_src(zip_map, current_file, path, depth, verbose, css_cache, css_lock)
            return f'url("{b64}")' if b64 else match.group(0)
        
        text = re.sub(r'url\((.*?)\)', css_url_repl, text)
        return text

    # For HTML files, separate head and body to apply specific rules
    if current_file.lower().endswith(('.html', '.htm')):
        parts = re.split(r'(</head>)', content, maxsplit=1, flags=re.IGNORECASE)
        if len(parts) == 3:
            # parts[0]: content before </head>, parts[1]: </head>, parts[2]: rest
            return apply_resolution(parts[0], is_head=True) + parts[1] + apply_resolution(parts[2], is_head=False)
    
    return apply_resolution(content)

def run_wkhtmltopdf(wkhtml_path, html_str, target, verbose_mode, part_info="", file_range_info="", log_enabled=False):
    if stop_event.is_set():
        return
    
    # Identify job ID from part_info for cleaner log
    job_id = part_info.split(' ')[1] if ' ' in part_info else part_info
    
    if not verbose_mode:
        print(f"  > [Job {job_id}] Started: {file_range_info}")

    cmd = [
        wkhtml_path, 
        "--enable-local-file-access", 
        "--encoding", "UTF-8",
        "--load-error-handling", "ignore",
        "--load-media-error-handling", "ignore"
    ]
    if not verbose_mode and not log_enabled:
        cmd.append("--quiet")
    cmd.extend(["-", target])

    process = subprocess.Popen(
        cmd, 
        stdin=subprocess.PIPE, 
        stdout=subprocess.PIPE, 
        stderr=subprocess.PIPE,
        text=True,
        encoding='utf-8',
        errors='replace'
    )
    
    # Log PID to prove parallelism
    if not verbose_mode:
        print(f"  > [Job {job_id}] Spawned process PID: {process.pid}")

    with procs_lock:
        active_procs.append(process)

    def writer():
        try:
            process.stdin.write(html_str)
            process.stdin.close()
        except Exception as e:
            if verbose_mode: print(f"Writer error in {part_info}: {e}")

    write_thread = threading.Thread(target=writer)
    write_thread.start()

    try:
        if stop_event.is_set():
            process.terminate()
            return

        while True:
            # Check for stop signal appropriately
            if stop_event.is_set():
                process.terminate()
                break
                
            line = process.stderr.readline()
            if not line:
                if process.poll() is not None:
                    break
                continue
                
            line = line.strip()
            if not line: continue
            
            status_update = ""
            if "Loading pages" in line: status_update = "Loading pages..."
            elif "Counting pages" in line: status_update = "Counting pages..."
            elif "Printing pages" in line: status_update = "Printing pages..."
            elif "Done" in line: status_update = "Done!"

            if verbose_mode:
                if status_update: print(f"  [{part_info}] [STATUS] {status_update}")
                print(f"    [{part_info}] [wkhtml] {line}")
            elif log_enabled:
                 print(f"    [Job {job_id}] {line}")
            else:
                if status_update:
                    # If parallel (part_info provided and likely >1 job), avoid \r to prevent overwrite mess
                    print(f"  > [Job {job_id}] {status_update}")

        process.wait()
        write_thread.join()
        
        with procs_lock:
            if process in active_procs:
                active_procs.remove(process)
        
        if stop_event.is_set():
            return

        if not verbose_mode: print(f"  > [Job {job_id}] Finished: Processed successfully.")

        if process.returncode != 0:
            print(f"Error: {part_info} failed with exit code {process.returncode}")
    except Exception as e:
        print(f"Error during PDF generation for {part_info}: {e}")
        process.kill()

def process_single_file(file_name, zip_map, verbose, ignore_icons, css_cache=None, css_lock=None, log_enabled=False, file_index=-1):
    if stop_event.is_set(): return None, 0
    
    # Get thread name for logging
    t_name = threading.current_thread().name
    
    ext = os.path.splitext(file_name.lower())[1]
    raw_data = zip_map[file_name]
    file_size = len(raw_data)
    
    content_result = None
    
    # HTML: Resolve internal links
    if ext in {'.html', '.htm'}:
        try:
            text = raw_data.decode('utf-8')
        except UnicodeDecodeError:
            text = raw_data.decode('latin-1')
        content_result = resolve_resources(zip_map, file_name, text, 0, verbose, css_cache, css_lock, ignore_icons)
    
    # Image: Wrap in HTML
    elif ext in {'.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg'}:
        mime_type = mimetypes.guess_type(file_name)[0] or 'image/png'
        b64 = base64.b64encode(raw_data).decode('utf-8')
        content_result = f'<div style="text-align:center; page-break-inside: avoid;"><img src="data:{mime_type};base64,{b64}" style="max-width:100%; height:auto;"></div>'
    
    # CSS: Injected
    elif ext == '.css':
        try:
            css = raw_data.decode('utf-8')
        except UnicodeDecodeError:
            css = raw_data.decode('latin-1')
        content_result = f'<style>{css}</style>'

    # Text: Pre-formatted
    elif ext in {'.txt', '.md', '.py', '.js', '.json', '.log', '.csv', '.xml', '.cs'}:
        try:
            text_content = raw_data.decode('utf-8')
        except UnicodeDecodeError:
            text_content = raw_data.decode('latin-1')
        text_content = text_content.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
        content_result = f'<pre style="white-space: pre-wrap; word-wrap: break-word; font-family: monospace; background: #f4f4f4; padding: 10px; border: 1px solid #ddd;">{text_content}</pre>'

    if log_enabled:
         # Shorten thread name to just the number if possible "ThreadPoolExecutor-0_0" -> "0_0"
         short_t = t_name.replace("ThreadPoolExecutor-", "T")
         idx_str = f"[#{file_index}]" if file_index >= 0 else ""
         print(f"  [Index]{idx_str}[{short_t}] Processed: {file_name}")

    return content_result, file_size

    return content_result, file_size

def process_batch_pipeline(batch_files, batch_idx, total_batches, zip_path, output_dir, output_basename, wkhtml_path, verbose, ignore_icons, log_enabled, global_start_idx=0):
    """
    Worker function that handles the full lifecycle of a single PDF batch.
    Run as a PROCESS. No global shared memory.
    """
    # Note: stop_event is global but not shared across processes easily on Windows without Manager.
    # We will ignore stop_event inside the child process for simplicity, 
    # relying on main process termination to kill children.

    # 0. Setup Local Environment
    # We need to read the specific files for this batch from the ZIP
    local_zip_map = {}
    
    try:
        if verbose or log_enabled:
            print(f"  [Batch {batch_idx}] Loading {len(batch_files)} files from zip...")
        
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for name in batch_files:
                local_zip_map[name] = zf.read(name)
        
        # Also need to be able to read CSS files if they are referenced recursively!
        # This is a bit tricky. process_single_file might ask for 'style.css' which is NOT in batch_files.
        # So we need a mechanism to read on-demand.
        # Optimized approach: Read ALL CSS files into local map? 
        # Or Just pass the ZipFile object? 
        # process_single_file expects a dict (zip_map).
        # We'll use a Hybrid: Pre-load batch files + On-demand lookup?
        # NO, process_single_file is synchronous.
        # Best approach for Process isolation: Load ALL CSS files into local map too.
        # This duplicates CSS memory but ensures safety.
        with zipfile.ZipFile(zip_path, 'r') as zf:
             for name in zf.namelist():
                 # Load if in batch or if it looks like a resource we might need (css/js/images?)
                 # Loading ALL images is too heavy (400MB).
                 # Loading ALL CSS is fine.
                 if name in batch_files: continue # Already loaded
                 if name.lower().endswith('.css'):
                     local_zip_map[name] = zf.read(name)
                 # What about images referenced by this batch?
                 # If file1.html tags img1.png. img1.png needs to be in local_zip_map.
                 # We don't know which images are needed until we parse.
                 # Solution: process_single_file needs to handle cache misses? 
                 # Currently it fails.
                 
                 # Correct Solution:
                 # process_single_file should take a "fallback accessor"?
                 
    except Exception as e:
        print(f"Error opening zip in worker: {e}")
        return

    # To handle lazy loading of images, we need to wrap the map.
    # But zip_map is expected to be a dict or support .get() and .items()
    # Let's verify process_single_file usage:
    # 1. raw_data = zip_map[file_name] (Direct access)
    # 2. get_b64_src -> find_in_zip -> zip_map.get(target)
    # 3. get_b64_src -> loop zip_map.items() (Strategy 3 global search)
    
    # Strategy 3 is EXPENSIVE if zip_map is empty.
    # Strategy 3 is IMPOSSIBLE if we don't load everything. (We can't global search what we don't have).
    
    # WORKAROUND: In Pipeline Mode, we assume well-formed paths or relative paths.
    # To support on-demand loading, we can make a SmartDict.
    
    class SmartZipDict(dict):
        def __init__(self, zpath, initial_files):
            self.zpath = zpath
            self.zf = zipfile.ZipFile(zpath, 'r') # Open and keep open?
            self.namelist_set = set(self.zf.namelist())
            # Preload initial
            for f in initial_files:
                 if f in self.namelist_set:
                     try: self[f] = self.zf.read(f)
                     except: pass
            
        def get(self, key, default=None):
            if key in self: return self[key]
            # Try to load
            if key in self.namelist_set:
                try: 
                    rs = self.zf.read(key)
                    self[key] = rs
                    return rs
                except: pass
            # Try to handle unix/windows path mismatch
            # (Limited fallback)
            return default

        def items(self):
            # For Strategy 3 (Global Search), this only iterates loaded items.
            # Strategy 3 will fail for unloaded items. This is an acceptable trade-off for speed/memory.
            return super().items()
            
    # Use Smart Dict
    local_zip_map = SmartZipDict(zip_path, batch_files)

    # 1. Indexing (Local to this batch)
    if verbose or log_enabled:
        print(f"  [Batch {batch_idx}/{total_batches}] Indexing {len(batch_files)} files...")

    batch_htmls = []
    local_css_cache = {} # Local empty cache

    for i, fname in enumerate(batch_files):
        # Stop event check is hard in process, skipping
        current_global_idx = global_start_idx + i
        # We catch exceptions here to prevent crash
        try:
             # Check if file exists in smart dict (might not if user provided bad list? No, came from namelist)
             if fname not in local_zip_map:
                 # Attempt load one last time
                 local_zip_map.get(fname)
                 
             content, _ = process_single_file(fname, local_zip_map, verbose, ignore_icons, local_css_cache, None, log_enabled, current_global_idx)
             if content:
                 batch_htmls.append(content)
        except Exception as e:
             if verbose: print(f"Error indexing {fname} in batch {batch_idx}: {e}")

    if not batch_htmls:
        print(f"  [Batch {batch_idx}] Warning: No valid content found.")
        return

    # 2. Combine
    combined_html = "\n<div style='page-break-after: always;'></div>\n".join(batch_htmls)

    # 3. Target Path
    name_part, ext_part = os.path.splitext(output_basename)
    if not ext_part: ext_part = ".pdf"
    
    filename = output_basename if total_batches == 1 else f"{name_part}_{batch_idx}{ext_part}"
    target_path = os.path.join(output_dir, filename)
    
    # 4. Generate PDF
    part_info = f"Part {batch_idx}/{total_batches}"
    file_range_msg = f"Files ({len(batch_files)} files)"
    
    # run_wkhtmltopdf also checks stop_event (global). 
    # In Process, stop_event is a copy of the state at fork (False).
    # It won't update. So Ctrl+C might leave zombies. Valid limitation for now.
    run_wkhtmltopdf(wkhtml_path, combined_html, target_path, verbose, part_info, file_range_msg, log_enabled)


def load_zip_chunk(source, file_names):
    """
    Worker function to read a subset of files from a zip source.
    source: can be a file path (str) or raw bytes (bytes)
    file_names: list of specific files to read
    """
    if stop_event.is_set(): return {}
    
    chunk_map = {}
    
    # If source is bytes, wrap in BytesIO. If path, just use path.
    # IMPORTANT: We create a NEW file-like object or handle for EVERY thread
    # to ensure thread safety (avoiding shared cursor position).
    if isinstance(source, bytes):
        context = io.BytesIO(source)
    else:
        context = source # File path string

    try:
        with zipfile.ZipFile(context, 'r') as zf:
            for name in file_names:
                if stop_event.is_set(): break
                chunk_map[name] = zf.read(name)
    except Exception as e:
        print(f"Error reading chunk: {e}")
        
    return chunk_map

def convert_zip_to_pdf(zip_path, output_dir, output_basename, max_size_mb=150, max_files=20, verbose=False, memory_mode=False, ignore_icons=False, jobs=4, files_per_pdf=0, pdf_jobs=None, info_mode=False, log_enabled=False):
    """
    Reads files from a ZIP and converts them into one or more PDF files in output_dir.
    """
    if not os.path.exists(zip_path):
        print(f"Error: ZIP file '{zip_path}' not found.")
        return

    # Ensure output directory exists
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        if verbose: print(f"Created output directory: {output_dir}")

    try:
        # Reset global state
        stop_event.clear()
        with procs_lock:
            active_procs.clear()

        if info_mode:
            print(f"Scanning ZIP file summary: {zip_path}")
            with zipfile.ZipFile(zip_path, 'r') as zf:
                file_list = [n for n in zf.namelist() if not n.endswith('/')]
            
            num_files = len(file_list)
            html_count = sum(1 for f in file_list if f.lower().endswith(('.html', '.htm')))
            img_count = sum(1 for f in file_list if f.lower().endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg')))
            css_count = sum(1 for f in file_list if f.lower().endswith('.css'))
            other_count = num_files - html_count - img_count - css_count
            
            print(f"{'-'*40}")
            print(f"Total files found: {num_files}")
            print(f"  - HTML/HTM: {html_count}")
            print(f"  - Images:   {img_count}")
            print(f"  - CSS:      {css_count}")
            print(f"  - Others:   {other_count}")
            print(f"{'-'*40}")

            if files_per_pdf > 0:
                est_parts = (num_files + files_per_pdf - 1) // files_per_pdf
                print(f"With --files-per-pdf {files_per_pdf}, this will generate approximately {est_parts} PDF parts.")
            
            return

        # --- Phase 1: Scanning/Reading ZIP ---
        print(f"\n=== Phase 1: Reading ZIP File ({zip_path}) ===")

        # 1. Map all files in the ZIP for cross-referencing
        # 1. Map all files in the ZIP for cross-referencing
        zip_map = {} # path -> binary data
        
        # Pre-read source if memory mode
        zip_source_data = None
        if memory_mode:
            if verbose: print(f"  [MEMORY] Reading entire ZIP into memory buffer...")
            with open(zip_path, 'rb') as f:
                zip_source_data = f.read() # Read as raw bytes
            # We will pass raw bytes to workers
            worker_source = zip_source_data
        else:
            # We will pass file path to workers
            worker_source = zip_path

        # Get file list first (fast)
        if verbose: print(f"  Scanning file list...")
        all_names = []
        # We need a temporary open just to get the namelist
        temp_source = io.BytesIO(zip_source_data) if zip_source_data else zip_path
        with zipfile.ZipFile(temp_source, 'r') as zf:
            for name in zf.namelist():
                if not name.endswith('/'):
                    all_names.append(name)

        num_files_total = len(all_names)
        print(f"  found {num_files_total} files. Loading content with {jobs} threads...")

        # Split into chunks for parallel loading
        chunk_size = (num_files_total + jobs - 1) // jobs
        chunks = [all_names[i:i + chunk_size] for i in range(0, num_files_total, chunk_size)]

        # Run parallel loading
        with ThreadPoolExecutor(max_workers=jobs) as executor:
            future_to_chunk = {executor.submit(load_zip_chunk, worker_source, chunk): chunk for chunk in chunks}
            
            for future in as_completed(future_to_chunk):
                if stop_event.is_set(): break
                try:
                    partial_map = future.result()
                    zip_map.update(partial_map)
                except Exception as e:
                    print(f"Error in zip loading worker: {e}")

        if stop_event.is_set(): return

        # Validate load
        if len(zip_map) != num_files_total:
             print(f"Warning: Loaded {len(zip_map)} files, expected {num_files_total}")

        all_files = sorted(zip_map.keys())
        num_all = len(all_files)
        
        # --- Phase 1.5: Pre-processing CSS ---
        # To avoid repeated processing and locking during HTML phase, we process ALL CSS first.
        css_files = [f for f in all_files if f.lower().endswith('.css')]
        other_files = [f for f in all_files if not f.lower().endswith('.css')]
        
        html_contents = [None] * num_all
        html_sizes = [0] * num_all

        # Prepare CSS cache
        css_cache = {}
        css_lock = threading.Lock()
        
        if css_files:
            print(f"\n=== Phase 1.5: Pre-resolving {len(css_files)} CSS files ===")
            with ThreadPoolExecutor(max_workers=jobs) as executor:
                # We don't need the results (html_contents) for CSS here, just side-effect of populating cache
                # But we should store them if they are needed as "content" (though usually CSS is embed-only)
                # Actually, zip2pdf includes CSS as separate pages if they are top-level files.
                # So we must map them back to their original index in 'all_files' if we want to include them in output.
                
                # Map filename to original index
                file_to_idx = {name: i for i, name in enumerate(all_files)}
                
                futures = {executor.submit(process_single_file, f, zip_map, verbose, ignore_icons, css_cache, css_lock, False, file_to_idx[f]): f for f in css_files}
                
                for future in as_completed(futures):
                    if stop_event.is_set(): break
                    fname = futures[future]
                    try:
                        content, size = future.result()
                        # Store result because CSS files themselves might be part of the PDF output
                        idx = file_to_idx[fname]
                        html_contents[idx] = content
                        html_sizes[idx] = size
                    except Exception as e:
                        print(f"Error pre-processing CSS {fname}: {e}")

        # --- Phase 2: Indexing ---
        print(f"\n=== Phase 2: Indexing and Resolving Resources ({len(other_files)} files) ===")
        # We only process non-CSS files here (images, html, etc)
        # Note: We technically re-process images if they are top-level files, 
        # but images are leaf nodes so no recursion/cache needed.
        
        # Determine Execution Strategy
        if files_per_pdf > 0:
            # --- PIPELINE MODE ---
            # We know exact batches upfront. We can run Index+Generate in parallel streams.
            print(f"  [Mode] Independent Pipeline (files-per-pdf={files_per_pdf})")
            
            # Split other_files into batches
            batches = [other_files[i:i + files_per_pdf] for i in range(0, len(other_files), files_per_pdf)]
            total_batches = len(batches)
            
            print(f"  Created {total_batches} batch jobs.")
            
            # Find wkhtmltopdf path (needed for workers)
            wkhtml_path = "wkhtmltopdf"
            default_win_path = r'C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe'
            if os.path.exists(default_win_path): wkhtml_path = default_win_path

            actual_pdf_jobs = pdf_jobs if pdf_jobs and pdf_jobs > 0 else jobs
            print(f"  Launching {actual_pdf_jobs} pipeline PROCESS workers...")
            
            with ProcessPoolExecutor(max_workers=actual_pdf_jobs) as executor:
                futures = []
                for i, batch in enumerate(batches, 1):
                    # Calculate start index for logging
                    start_idx = (i - 1) * files_per_pdf
                    # Pass zip_path instead of zip_map
                    # Pass None for css_cache (worker builds its own)
                    futures.append(executor.submit(process_batch_pipeline, 
                                                   batch, i, total_batches, 
                                                   zip_path, output_dir, output_basename, 
                                                   wkhtml_path, verbose, ignore_icons, 
                                                   log_enabled, start_idx))
                
                # Wait
                for future in as_completed(futures):
                    # Note: Stop event check here only stops MAIN process from waiting.
                    # Children will keep running unless simple kill.
                    if stop_event.is_set(): break
                    try: 
                        future.result()
                    except Exception as e:
                        print(f"Pipeline job failed: {e}")
            
            if not stop_event.is_set():
                print("\nAll pipeline jobs completed successfully.")
            return
            # --- END PIPELINE MODE ---

        # --- STANDARD MODE (Index All -> Split by Size) ---
        
        with ThreadPoolExecutor(max_workers=jobs) as executor:
            # Only submit 'other_files'
            file_to_idx = {name: i for i, name in enumerate(all_files)} # Re-use map
            
            # Pass None for css_lock to ensure Phase 2 never attempts to lock/write to cache
            future_to_idx = {executor.submit(process_single_file, f, zip_map, verbose, ignore_icons, css_cache, None, log_enabled, file_to_idx[f]): file_to_idx[f] for f in other_files}
            indexed_count = 0
            
            num_others = len(other_files)
            for future in as_completed(future_to_idx):
                if stop_event.is_set(): break
                idx = future_to_idx[future]
                try:
                    content, size = future.result()
                    if content:
                        html_contents[idx] = content
                        html_sizes[idx] = size
                except Exception as e:
                    if verbose: print(f"Error processing {all_files[idx]}: {e}")
                
                # Logging moved inside process_single_file to capture thread ID correctly
                
                indexed_count += 1
                if not verbose and not log_enabled and (indexed_count % 10 == 0 or indexed_count == num_others):
                    print(f"\r  > Indexing files... [{indexed_count}/{num_others}]", end="", flush=True)
        
        if stop_event.is_set():
            print("\nOperation cancelled.")
            return

        print() # Newline after progress bar

        # Remove skipped files (where content_result was None)
        html_contents = [c for c in html_contents if c is not None]
        html_sizes = [s for s in html_sizes if s > 0] # Ensure size is also valid

        total_size = sum(html_sizes)

        # --- Phase 3: Batching ---
        print(f"\n=== Phase 3: Creating PDF Batches ===")

        # Calculate batch size
        limit_bytes = int(max_size_mb * 1024 * 1024 * 0.9)
        parts_by_size = (total_size + limit_bytes - 1) // limit_bytes
        
        if parts_by_size > max_files:
            print(f"Warning: Total size ({total_size/1024/1024:.1f}MB) exceeds limit for {max_files} files. Adjusting batch threshold.")
            batch_threshold = (total_size + max_files - 1) // max_files
        else:
            batch_threshold = limit_bytes

        print(f"Total size: {total_size/1024/1024:.1f}MB. Target parts: {min(max(1, parts_by_size), max_files)}.")

        name_part, ext_part = os.path.splitext(output_basename)
        if not ext_part:
            ext_part = ".pdf"
        
        # Prepare batches
        batches = []
        
        if files_per_pdf > 0:
            # Enforce parallelization by explicitly splitting into chunks of files_per_pdf
            print(f"Splitting into batches of {files_per_pdf} files each (ignoring count/limit auto-split)...")
            for i in range(0, len(html_contents), files_per_pdf):
                batches.append(html_contents[i : i + files_per_pdf])
        else:
            # Automatic size-based batching
            current_batch = []
            current_size = 0
            
            for content, size in zip(html_contents, html_sizes):
                if current_batch and (current_size + size > batch_threshold) and (len(batches) < max_files - 1):
                    batches.append(current_batch)
                    current_batch = [content]
                    current_size = size
                else:
                    current_batch.append(content)
                    current_size += size
            
            if current_batch:
                batches.append(current_batch)

        # Find wkhtmltopdf path
        wkhtml_path = "wkhtmltopdf" # Default in PATH
        default_win_path = r'C:\Program Files\wkhtmltopdf\bin\wkhtmltopdf.exe'
        if os.path.exists(default_win_path):
            wkhtml_path = default_win_path

        # Generate PDFs in parallel
        # Use specific pdf_jobs if provided, otherwise default to general jobs count
        actual_pdf_jobs = pdf_jobs if pdf_jobs and pdf_jobs > 0 else jobs
        
        # --- Phase 4: Generation ---
        print(f"\n=== Phase 4: Generating PDFs ===")
        print(f"Generating {len(batches)} PDF parts in parallel (max {actual_pdf_jobs} workers)...")
        with ThreadPoolExecutor(max_workers=actual_pdf_jobs) as executor:
            current_file_idx = 0
            futures = []
            for i, batch in enumerate(batches, 1):
                filename = output_basename if len(batches) == 1 else f"{name_part}_{i}{ext_part}"
                target_path = os.path.join(output_dir, filename)
                
                # Calculate file range for this batch
                start_idx = current_file_idx + 1
                end_idx = current_file_idx + len(batch)
                file_range_msg = f"Files {start_idx}-{end_idx} ({len(batch)} files)"
                current_file_idx = end_idx
                
                # Add page break between documents in a batch
                combined_html = "\n<div style='page-break-after: always;'></div>\n".join(batch)
                
                futures.append(executor.submit(run_wkhtmltopdf, wkhtml_path, combined_html, target_path, verbose, f"Part {i}/{len(batches)}", file_range_msg, log_enabled))
            
            for future in as_completed(futures):
                if stop_event.is_set(): break
                # Wait for each future to complete and handle any exceptions
                try:
                    future.result()
                except Exception as exc:
                    print(f"PDF generation generated an exception: {exc}")

        if stop_event.is_set():
            print("\nOperation cancelled during PDF generation.")
        else:
            print("\nAll conversions completed successfully.")

    except KeyboardInterrupt:
        print("\n\n[Ctrl+C] Stopping... Please wait for cleanup.")
        stop_event.set()
        
        # Kill active child processes
        with procs_lock:
            for p in active_procs:
                if p.poll() is None:
                    try:
                        p.terminate()
                    except:
                        pass
        try:
             # Give them a moment to die gracefully
             pass
        except:
            pass
        
        sys.exit(1)

    except Exception as e:
        print(f"An error occurred: {e}")

def main():
    parser = argparse.ArgumentParser(description="Convert HTML/Images in a ZIP to PDF files in a specified folder.")
    parser.add_argument("input_zip", help="Path to the source ZIP file")
    parser.add_argument("output_dir", help="Directory where the PDF files will be saved")
    parser.add_argument("output_basename", help="Base filename for the output PDF(s)")
    parser.add_argument("--max-size", type=float, default=150, help="Max size of each PDF in MB (default: 150)")
    parser.add_argument("--max-files", type=int, default=20, help="Max number of split PDF files (default: 20)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show detailed progress information")
    parser.add_argument("-c", "--memory-mode", action="store_true", help="Read entire ZIP file into memory before processing")
    parser.add_argument("-x", "--ignore-icons", action="store_true", help="Ignore favicon and header section icons")
    parser.add_argument("-j", "--jobs", type=int, default=4, help="Number of parallel jobs (default: 4)")
    parser.add_argument("--files-per-pdf", type=int, default=0, help="Enforce specific number of files per PDF (overrides auto-splitting)")
    parser.add_argument("--pdf-jobs", type=int, default=0, help="Number of parallel wkhtmltopdf instances (default: same as --jobs)")
    parser.add_argument("--info", action="store_true", help="Display file count and details without converting")
    parser.add_argument("--log", action="store_true", help="Show processing log messages (filenames and wkhtmltopdf output)")

    args = parser.parse_args()
    convert_zip_to_pdf(args.input_zip, args.output_dir, args.output_basename, args.max_size, args.max_files, args.verbose, args.memory_mode, args.ignore_icons, args.jobs, args.files_per_pdf, args.pdf_jobs, args.info, args.log)

if __name__ == "__main__":
    main()
