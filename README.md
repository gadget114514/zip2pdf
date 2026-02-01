
# Zip2PDF

A high-performance Python utility to convert HTML files and assets inside a ZIP archive into PDF files without the need for manual extraction.

## Features

-   **Zero Extraction**: Processes files directly from the ZIP archive (supports both disk-based and in-memory modes).
-   **Asset Embedding**: Automatically resolves and embeds local images, CSS, and fonts as Base64 data (supporting recursive CSS imports).
-   **Smart Splitting**: splits large outputs into multiple PDF files based on:
    -   Target File Size (e.g., 150MB per PDF)
    -   File Count (e.g., 50 HTML files per PDF)
-   **Parallel Processing**:
    -   Multi-threaded indexing and resource resolution (`-j`).
    -   Parallel execution of `wkhtmltopdf` processes for maximum throughput (`--pdf-jobs`).
-   **Robustness**:
    -   CSS Caching to prevent redundant processing.
    -   Graceful `Ctrl+C` interruption handling.
    -   Detailed logging phases and progress tracking.

## Requirements

-   **Python 3.6+**
-   **wkhtmltopdf** (Must be installed and added to PATH)
    -   [Download wkhtmltopdf](https://wkhtmltopdf.org/downloads.html)
-   Python Libraries:
    -   `pdfkit`

## Installation

1.  Clone this repository or download the script.
2.  Install the python dependency:

```bash
pip install pdfkit
```

3.  Ensure `wkhtmltopdf` is installed.

## Usage

### Basic Usage

Convert a ZIP file into a single PDF (or split automatically if too large):

```bash
python zip2pdf.py input.zip output_folder my_document_name
```

### Optimize for Speed (Parallel Processing)

For large documentation sets, use multiple threads for indexing and running `wkhtmltopdf` instances:

```bash
# Use 8 threads for indexing, and run 4 wkhtmltopdf processes in parallel
python zip2pdf.py input.zip output_folder doc_name --jobs 8 --pdf-jobs 4
```

### Precise Batch Control

Force specific batch sizes (e.g., create a new PDF for every 50 HTML files):

```bash
python zip2pdf.py input.zip output_folder doc_name --files-per-pdf 50
```

### Check Statistics (Dry Run)

See how many files are inside and how many PDF parts would be generated:

```bash
python zip2pdf.py input.zip . doc_name --info --files-per-pdf 50
```

### Full Options List

```text
positional arguments:
  input_zip             Path to the source ZIP file
  output_dir            Directory where the PDF files will be saved
  output_basename       Base filename for the output PDF(s)

options:
  -h, --help            show this help message
  --max-size MAX_SIZE   Max size of each PDF in MB (default: 150)
  --max-files MAX_FILES Max number of split PDF files (default: 20)
  -v, --verbose         Show detailed internal debug logs
  -c, --memory-mode     Read entire ZIP file into RAM (faster for small/medium zips)
  -x, --ignore-icons    Ignore favicons and header icons
  -j, --jobs N          Number of threads for indexing (default: 4)
  --pdf-jobs N          Number of parallel wkhtmltopdf instances (default: same as -j)
  --files-per-pdf N     Enforce specific files per PDF (overrides size auto-split)
  --info                Display summary stats without converting
  --log                 Show active process filenames and wkhtmltopdf output
```

## How It Works

1.  **Phase 1: Reading**: Scans the ZIP map.
2.  **Phase 2: Indexing**: Resolves all images/CSS into Base64 strings. Caches repeated CSS.
3.  **Phase 3: Batching**: Groups files based on size limits or count limits.
4.  **Phase 4: Generation**: Spawns multiple `wkhtmltopdf` subprocesses to render the batches into PDFs.

## License

MIT

