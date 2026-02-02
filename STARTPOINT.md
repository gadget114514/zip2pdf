# Zip2PDF

A Windows console application to convert HTML files inside a ZIP archive into a single PDF file without extracting them to disk.

## Requirements

- Python 3.x
- `pdfkit` library: `pip install pdfkit`
- `wkhtmltopdf` (must be installed and added to PATH)

## Options

- `--max-size`: Max size of each PDF in MB (default: 150)
- `--max-files`: Max number of split PDF files (default: 20)
- `-j`, `--jobs`: Number of threads for file indexing/resolving (default: 4)
- `--files-per-pdf`: Enforce a specific number of files per PDF part (overrides auto-splitting logic)
- `--pdf-jobs`: Number of parallel `wkhtmltopdf` processes (defaults to same as `--jobs`)
- `--info`: Scan and display file statistics without running conversion
- `-v`: Verbose mode (show detailed indexing and resolution logs)
- `-c`: Memory mode (read entire ZIP into RAM first)
- `-x`: Ignore icons (skips favicons and known icon files)
- `-t`, `--save-temp`: Save intermediate HTML files to output directory
- `--html-only`: Only save intermediate HTML files and skip PDF conversion

## Usage

```bash
# Basic usage
python zip2pdf.py input.zip output_folder result_name

# Advanced parallel processing breakdown
python zip2pdf.py input.zip output_folder result_name --files-per-pdf 50 --pdf-jobs 4 --jobs 8

# Check statistics only
python zip2pdf.py input.zip . name --info
```
