# HiFi DVD Maker

<p align="center">
  <img src="data/screenshots/main.png" alt="HiFi DVD Maker Screenshot" width="700"/>
</p>

**HiFi DVD Maker** is a modern GTK4 / Libadwaita desktop application designed for Linux to easily author and create high-fidelity audio DVDs with custom cover art for your music compilation.

## Features

- **High-Fidelity Audio Support**: Author uncompressed, CD-quality (LPCM) audio DVDs with pristine fidelity.
- **AC3 Compression Option**: Optional Dolby Digital (AC3) audio encoding support for maximum compatibility and space savings.
- **Multiple Output Formats**: Export your creation as an ISO disc image file or directly as a `VIDEO_TS` folder structure.
- **Standalone DVD Navigation**: Generates standard DVD tracks/chapters that allow skipping forward and backward on any home DVD player or car system using the remote control.
- **Compilation Cover Art**: Easily upload custom cover art to display as the background for your music compilation disc.
- **Multilingual Support**: Available in 12 languages.
- **GNOME Native**: Crafted with GTK4 and Libadwaita following the latest GNOME Human Interface Guidelines (HIG).
- **Flatpak Integration**: Fully sandboxed and ready for modern Linux distributions.

## Installation & Building

### Prerequisites
Make sure you have `flatpak` and `flatpak-builder` installed on your system.

### Local Build & Test
To build and install the application locally:

```bash
# Build and install the Flatpak package locally
flatpak-builder --user --install --force-clean build-dir io.github.marcopcabrera.HiFiDvdMaker.json

# Run the installed application
flatpak run io.github.marcopcabrera.HiFiDvdMaker
