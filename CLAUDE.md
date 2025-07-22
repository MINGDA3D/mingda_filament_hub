# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This is the **MINGDA Filament Hub** (MINGDA料丝中心) - a Python-based automation system for 3D printers that automatically manages filament feeding through CAN bus communication with Klipper firmware.

## Development Commands

### Installation and Setup
```bash
# Install system dependencies
sudo apt install -y python3-pip python3-yaml python3-can

# Install Python package in development mode
pip3 install -e .

# Run the application
mingda_filament_hub -c config/config.yaml  # With specific config
mingda_filament_hub -v                      # Verbose mode for debugging
python -m mingda_filament_hub               # As Python module
```

### Service Management
```bash
# Install as systemd service
sudo scripts/install.sh

# Service control
sudo systemctl start mingda_filament_hub
sudo systemctl status mingda_filament_hub
sudo systemctl stop mingda_filament_hub
sudo journalctl -u mingda_filament_hub -f  # View logs
```

### CAN Bus Debugging
```bash
# Monitor CAN traffic
candump can1                           # Monitor all CAN messages
candump can1 | grep -E "(10A|10B)"   # Monitor filament hub messages
```

## Architecture Overview

### Core Components

1. **main.py**: Application entry point that orchestrates all components
   - Loads configuration from YAML
   - Initializes CAN communication and Klipper monitoring
   - Manages the main event loop

2. **can_communication.py**: CAN bus communication module
   - Handles all CAN protocol implementation
   - Message queuing and thread-safe operations
   - Protocol: Printer→Hub (0x10A), Hub→Printer (0x10B)
   - Implements handshake, status queries, and command sending

3. **klipper_monitor.py**: Klipper/Moonraker integration
   - WebSocket connection for real-time status updates
   - REST API calls for printer control
   - Monitors filament sensors and printer state
   - Handles print pause/resume operations

### Communication Flow

```
Klipper/Moonraker <--WebSocket/REST--> MINGDAFilamentHubApp <--CAN Bus--> MINGDA Filament Hub Controller
                                              |
                                              v
                                    State Management & Logic
```

### Key Design Patterns

- **Thread Safety**: Uses queues and thread pools for concurrent operations
- **State Management**: Explicit state tracking for both printer and feeder
- **Error Recovery**: Automatic retry with configurable attempts
- **Event-Driven**: Responds to filament runout events and status changes

## Important Notes

- The system uses Chinese documentation and comments
- CAN communication runs at 1Mbps (configurable)
- Dual extruder support with configurable tube mappings
- All logging goes to `/home/mingda/printer_data/logs/`
- Configuration file at `/etc/mingda_filament_hub/config.yaml`
- G-code commands are integrated via Klipper macros

## Testing & Debugging

When debugging issues:
1. Enable verbose logging with `-v` flag
2. Check CAN communication with `candump`
3. Monitor WebSocket connection status
4. Verify Moonraker API accessibility
5. Check systemd service logs with `journalctl`

## Common Tasks

- **Adding new CAN commands**: Modify protocol constants in can_communication.py
- **Changing status update intervals**: Edit config.yaml timing parameters
- **Debugging connection issues**: Check CAN interface status and Moonraker URL
- **Modifying G-code commands**: Update command handlers in main.py