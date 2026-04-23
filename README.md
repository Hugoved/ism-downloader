# ISM Downloader

A Python-based tool for downloading and reconstructing media from **Microsoft Smooth Streaming (ISM)** manifests. The project parses the manifest, retrieves media fragments, and rebuilds a valid **MP4 file** with proper **PIFF headers** and DRM-related metadata.

---

## Features

* Parses **Smooth Streaming (ISM)** manifests
* Downloads and reconstructs **video and audio tracks**
* Generates valid **MP4/PIFF initialization segments**
* Supports **DRM metadata (PlayReady / Widevine PSSH)**
* Automatic bitrate probing for better track selection
* Interactive track selection

---

## Requirements

* Python 3.9+
* `requests`
* `inquirer` (optional, for interactive menu)

Installation:

```bash
pip install requests inquirer
```

---

## Usage

```bash
python ism_downloader.py
```

Then provide the manifest URL when prompted.

---

## Notes

This project was developed primarily as a personal initiative to better understand **Smooth Streaming (ISM)** and MP4 structure.
Some features were added to expand the scope of the project beyond its initial purpose.

---

## Issues and Support

If you encounter any issues, please open an issue in the repository.
Support and maintenance will be provided as time permits.

---

## Acknowledgements

Thank you for your interest in this project.
