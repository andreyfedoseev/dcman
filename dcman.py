#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "textual>=7.0",
#     "pyyaml>=6.0",
# ]
# ///

"""
Docker Compose Manager - A TUI tool to manage multiple docker-compose projects
"""

import argparse
import asyncio
import contextlib
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

import yaml
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.events import Key
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Static


@dataclass
class Service:
    """Represents a Docker service"""

    name: str
    project_name: str
    project_path: Path
    compose_file: Path
    status: str = "unknown"

    @property
    def full_name(self) -> str:
        """Returns the full container name"""
        return f"{self.project_name}-{self.name}"


class DockerComposeManager:
    """Manages docker-compose operations"""

    @staticmethod
    async def run_docker_command_async(
        cmd: list[str],
        cwd: Path,
        timeout: int = 60,
        stream_callback: Callable[[str], None] | None = None,
        max_lines: int | None = None,
        combine_stderr: bool = False,
    ) -> tuple[int, str, str]:
        """
        Run a docker command asynchronously and return (returncode, stdout, stderr)

        Args:
            cmd: Command to execute
            cwd: Working directory
            timeout: Timeout in seconds
            stream_callback: Optional callback called with each line of output
            max_lines: If set, keep only the last N lines of output
            combine_stderr: If True, combine stderr with stdout
                (for streaming build output)
        """
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT
                if combine_stderr
                else asyncio.subprocess.PIPE,
            )

            try:
                # If streaming is enabled, read line by line
                if stream_callback is not None or max_lines is not None:
                    # Use deque with maxlen for automatic line limiting
                    lines = deque(maxlen=max_lines) if max_lines else deque()

                    if process.stdout:
                        while True:
                            line = await asyncio.wait_for(
                                process.stdout.readline(), timeout=timeout
                            )
                            if not line:
                                break

                            decoded_line = line.decode("utf-8", errors="replace")
                            lines.append(decoded_line)

                            # Call the callback with each line if provided
                            if stream_callback:
                                stream_callback(decoded_line)

                    # Wait for process to complete
                    await process.wait()

                    stdout = "".join(lines)
                    stderr = ""

                else:
                    # Normal mode: read all at once
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(), timeout=timeout
                    )
                    stdout = stdout.decode()
                    stderr = stderr.decode() if stderr else ""

                returncode = (
                    process.returncode if process.returncode is not None else -1
                )
                return returncode, stdout, stderr

            except asyncio.TimeoutError:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
                return -1, "", f"Command timed out after {timeout} seconds"
            except asyncio.CancelledError:
                # Handle task cancellation - kill the process and wait for cleanup
                if process.returncode is None:
                    process.kill()
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(process.wait(), timeout=2.0)
                raise
        except asyncio.CancelledError:
            raise  # Re-raise cancellation
        except Exception as e:
            return -1, "", str(e)

    @staticmethod
    def find_compose_files(root_path: Path) -> list[Path]:
        """
        Find all docker-compose.yml files in subdirectories,
        excluding .devcontainer folders
        """
        compose_files = []
        for pattern in ["docker-compose.yml", "docker-compose.yaml"]:
            for file in root_path.rglob(pattern):
                # Skip files in .devcontainer folders
                if ".devcontainer" not in file.parts:
                    compose_files.append(file)
        return compose_files

    @staticmethod
    def parse_compose_file(compose_file: Path) -> tuple[str, list[str]]:
        """Parse a docker-compose file and extract service names"""
        try:
            with open(compose_file) as f:
                compose_data = yaml.safe_load(f)

            services = list(compose_data.get("services", {}).keys())
            project_name = compose_file.parent.name

            return project_name, services
        except Exception as e:
            print(f"Error parsing {compose_file}: {e}")
            return "", []

    @staticmethod
    async def get_service_status_async(project_path: Path, service_name: str) -> str:
        """Get the status of a service asynchronously"""
        try:
            (
                returncode,
                stdout,
                _stderr,
            ) = await DockerComposeManager.run_docker_command_async(
                ["docker", "compose", "ps", "-q", service_name],
                cwd=project_path,
                timeout=5,
            )

            if returncode != 0 or not stdout.strip():
                return "stopped"

            container_id = stdout.strip()
            (
                returncode,
                stdout,
                _stderr,
            ) = await DockerComposeManager.run_docker_command_async(
                ["docker", "inspect", "-f", "{{.State.Status}}", container_id],
                cwd=project_path,
                timeout=5,
            )

            return stdout.strip() or "unknown"
        except Exception:
            return "unknown"

    @staticmethod
    async def execute_action_async(
        project_path: Path, service_name: str, action: str
    ) -> tuple[bool, str]:
        """Execute an action (start/stop/restart) on a service asynchronously"""
        try:
            if action == "start":
                cmd = ["docker", "compose", "up", "-d", service_name]
            elif action == "stop":
                cmd = ["docker", "compose", "stop", service_name]
            elif action == "restart":
                cmd = ["docker", "compose", "restart", service_name]
            else:
                return False, f"Unknown action: {action}"

            (
                returncode,
                _stdout,
                stderr,
            ) = await DockerComposeManager.run_docker_command_async(
                cmd, cwd=project_path, timeout=60
            )

            if returncode == 0:
                return True, f"Successfully {action}ed {service_name}"
            else:
                return False, f"Error: {stderr}"
        except Exception as e:
            return False, f"Exception: {e!s}"

    @staticmethod
    async def build_service_async(
        project_path: Path, service_name: str
    ) -> tuple[bool, str]:
        """Build (or rebuild) a service asynchronously"""
        try:
            cmd = ["docker", "compose", "build", service_name]

            (
                returncode,
                _stdout,
                stderr,
            ) = await DockerComposeManager.run_docker_command_async(
                cmd,
                cwd=project_path,
                timeout=300,  # Building can take longer
            )

            if returncode == 0:
                return True, f"Successfully built {service_name}"
            else:
                return False, f"Error: {stderr}"
        except Exception as e:
            return False, f"Exception: {e!s}"

    @staticmethod
    async def build_service_streaming_async(
        project_path: Path,
        service_name: str,
        log_callback: Callable[[str], None] | None = None,
    ) -> tuple[bool, str]:
        """Build a service and stream output to a callback"""
        try:
            cmd = ["docker", "compose", "build", service_name]

            (
                returncode,
                _stdout,
                _stderr,
            ) = await DockerComposeManager.run_docker_command_async(
                cmd,
                cwd=project_path,
                timeout=300,  # Building can take longer
                stream_callback=log_callback,
                max_lines=200,  # Keep last 200 lines in memory
                combine_stderr=True,  # Combine stderr with stdout for build output
            )

            if returncode == 0:
                return True, f"Successfully built {service_name}"
            else:
                return False, f"Build failed with exit code {returncode}"

        except asyncio.CancelledError:
            raise
        except Exception as e:
            return False, f"Exception: {e!s}"

    @staticmethod
    async def get_service_logs_async(
        project_path: Path, service_name: str, tail: int = 100
    ) -> str:
        """Get logs for a service asynchronously"""
        try:
            (
                returncode,
                stdout,
                stderr,
            ) = await DockerComposeManager.run_docker_command_async(
                ["docker", "compose", "logs", "--tail", str(tail), service_name],
                cwd=project_path,
                timeout=10,
                max_lines=tail,  # Keep only the requested number of lines
            )

            if returncode == 0:
                return stdout if stdout else "No logs available"
            else:
                return f"Error fetching logs: {stderr}"
        except Exception as e:
            return f"Exception: {e!s}"


class LogsScreen(ModalScreen):
    """Modal screen to display service logs"""

    CSS = """
    LogsScreen {
        align: center middle;
    }

    #logs-container {
        width: 90%;
        height: 90%;
        border: thick $primary;
        background: $surface;
    }

    #logs-title {
        dock: top;
        height: 3;
        background: $primary;
        color: $text;
        content-align: center middle;
        text-style: bold;
    }

    #logs-scroll {
        height: 1fr;
    }

    #logs-content {
        padding: 1;
    }

    #logs-footer {
        dock: bottom;
        height: 3;
        background: $boost;
        content-align: center middle;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,q", "dismiss", "Close"),
    ]

    def __init__(
        self,
        service: Service,
        logs: str,
        manager: "DockerComposeManager",
        build_logs_ref: dict | None = None,
        log_type: str = "container",
    ) -> None:
        super().__init__()
        self.service = service
        self.logs = logs
        self.manager = manager
        self.build_logs_ref = build_logs_ref if build_logs_ref is not None else {}
        self.log_type = log_type  # "container" or "build"
        self.refresh_timer = None

    def compose(self) -> ComposeResult:
        with Vertical(id="logs-container"):
            log_type_display = (
                "Build Logs" if self.log_type == "build" else "Container Logs"
            )
            yield Static(
                f"{log_type_display}: {self.service.project_name}/{self.service.name}",
                id="logs-title",
            )
            with VerticalScroll(id="logs-scroll"):
                # Disable markup to show logs as plain text
                logs_widget = Static(self.logs, id="logs-content")
                logs_widget._render_markup = False
                yield logs_widget
            yield Static(
                "Press ESC or Q to close | Logs refresh every second", id="logs-footer"
            )

    def on_mount(self) -> None:
        """Scroll to bottom when mounted and start auto-refresh"""
        # Schedule scroll to bottom after content is rendered
        self.call_after_refresh(self.scroll_to_bottom)
        # Start auto-refresh timer (refresh every 1 second)
        self.refresh_timer = self.set_interval(1.0, self.refresh_logs)

    def on_unmount(self) -> None:
        """Stop auto-refresh when unmounted"""
        if self.refresh_timer is not None:
            self.refresh_timer.stop()

    async def refresh_logs(self) -> None:
        """Fetch and update logs"""
        # Check if we're at the bottom before refresh
        scroll = self.query_one("#logs-scroll", VerticalScroll)
        was_at_bottom = scroll.scroll_y >= scroll.max_scroll_y - 1

        # Determine which logs to fetch based on log type
        if self.log_type == "build":
            # Get build logs from the shared reference
            service_key = f"{self.service.project_name}/{self.service.name}"
            new_logs = self.build_logs_ref.get(service_key, None)

            # If build is complete (no longer in build_logs),
            # keep showing the last logs we have
            if new_logs is None:
                # Build finished - stop refreshing and keep the current logs displayed
                if self.refresh_timer is not None:
                    self.refresh_timer.stop()
                # Update title to indicate build is complete
                title = self.query_one("#logs-title", Static)
                title.update(
                    f"Build Logs (Complete): "
                    f"{self.service.project_name}/{self.service.name}"
                )
                return  # Don't update logs, keep showing what we have
        else:
            # Fetch container logs asynchronously
            new_logs = await self.manager.get_service_logs_async(
                self.service.project_path,
                self.service.name,
                200,  # Keep last 200 lines
            )

        # Update the logs content
        logs_widget = self.query_one("#logs-content", Static)
        logs_widget.update(new_logs)

        # Auto-scroll to bottom if we were already at the bottom
        if was_at_bottom:
            self.call_after_refresh(self.scroll_to_bottom)

    def scroll_to_bottom(self) -> None:
        """Scroll the logs to the bottom"""
        scroll = self.query_one("#logs-scroll", VerticalScroll)
        scroll.scroll_end(animate=False)


class ServiceList(DataTable):
    """Custom DataTable for displaying services"""

    def on_key(self, event: Key) -> None:
        """Handle key presses in the table"""
        if event.key == "enter":
            # Toggle the selected service when Enter is pressed
            if self.cursor_row < self.row_count:
                row_key = self.get_row_at(self.cursor_row)[0]
                self.post_message(self.RowSelected(self, self.cursor_row, row_key))
            event.prevent_default()
            event.stop()


class StatusBar(Static):
    """Status bar to show messages"""

    message = reactive("")

    def watch_message(self, message: str) -> None:
        self.update(message)


class DockerComposeManagerApp(App):
    """A Textual app to manage Docker Compose services"""

    CSS = """
    Screen {
        background: $surface;
    }

    #main-container {
        height: 100%;
        padding: 1;
    }

    #service-table {
        height: 1fr;
        margin-bottom: 1;
    }

    #button-container {
        height: auto;
        align: center middle;
        padding: 1;
    }

    Button {
        margin: 0 1;
    }

    #status-bar {
        height: 3;
        background: $boost;
        padding: 1;
        text-align: center;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh_service_list", "Refresh"),
        Binding("s", "start_service", "Start"),
        Binding("t", "stop_service", "Stop"),
        Binding("e", "restart_service", "Restart"),
        Binding("b", "build_service", "Build"),
        Binding("l", "open_logs", "Logs"),
    ]

    def __init__(self, root_path: Path | None = None) -> None:
        super().__init__()
        self.services: list[Service] = []
        self.manager = DockerComposeManager()
        self.root_path = root_path or Path.cwd()
        self.service_to_row_key: dict[int, object] = {}  # Maps service index to row key
        self.build_logs: dict[str, str] = {}  # Maps service key to build logs
        self.build_processes: dict[
            str, asyncio.subprocess.Process
        ] = {}  # Active build processes

    def compose(self) -> ComposeResult:
        """Create child widgets"""
        yield Header()
        with Vertical(id="main-container"):
            yield ServiceList(id="service-table", zebra_stripes=True, cursor_type="row")
            with Horizontal(id="button-container"):
                yield Button("Start", variant="success", id="btn-start")
                yield Button("Stop", variant="error", id="btn-stop")
                yield Button("Restart", variant="warning", id="btn-restart")
                yield Button("Build", variant="default", id="btn-build")
                yield Button("Logs", variant="default", id="btn-logs")
                yield Button("Refresh", variant="primary", id="btn-refresh")
            yield StatusBar(id="status-bar")
        yield Footer()

    def on_mount(self) -> None:
        """Initialize the app"""
        table = self.query_one("#service-table", ServiceList)
        table.add_columns("Project", "Service", "Status")

        # Show empty table immediately
        self.set_status("Scanning for docker-compose files...")

        # Load services in background
        self.run_worker(self.load_services_async(), exclusive=True)

    async def load_services_async(self) -> None:
        """Load all services from docker-compose files asynchronously"""
        loop = asyncio.get_event_loop()

        # Find compose files in background
        compose_files = await loop.run_in_executor(
            None, self.manager.find_compose_files, self.root_path
        )

        if not compose_files:
            self.set_status(f"No docker-compose files found in {self.root_path}")
            return

        self.set_status(
            f"Found {len(compose_files)} docker-compose projects, loading services..."
        )

        # Process each compose file separately
        for compose_file in compose_files:
            await self.load_project_async(compose_file)

        self.set_status(
            f"Found {len(self.services)} services in "
            f"{len(compose_files)} projects. Ready."
        )

    async def load_project_async(self, compose_file: Path) -> None:
        """Load a single project's services asynchronously"""
        loop = asyncio.get_event_loop()

        # Parse compose file
        project_name, service_names = await loop.run_in_executor(
            None, self.manager.parse_compose_file, compose_file
        )

        if not service_names:
            return

        # Add services to the list with "loading" status
        project_services = []
        start_index = len(self.services)  # Track starting index before adding

        for service_name in service_names:
            service = Service(
                name=service_name,
                project_name=project_name,
                project_path=compose_file.parent,
                compose_file=compose_file,
                status="loading",
            )
            self.services.append(service)
            project_services.append(service)

        # Add to table immediately with loading status
        self.add_services_to_table(project_services, start_index)

        # Fetch status for each service in background
        await self.refresh_project_status_async(project_name)

    def add_services_to_table(self, services: list[Service], start_index: int) -> None:
        """Add services to the table"""
        table = self.query_one("#service-table", ServiceList)

        for i, service in enumerate(services):
            status_display = self.format_status(service.status)
            # add_row returns the row key - we need to store it properly
            row_key = table.add_row(service.project_name, service.name, status_display)
            # Map service index to row key using the proper index
            service_idx = start_index + i
            self.service_to_row_key[service_idx] = row_key

    def rebuild_table(self) -> None:
        """Rebuild the entire table from current service data"""
        table = self.query_one("#service-table", ServiceList)

        # Remember cursor position and scroll offset
        old_cursor = table.cursor_row if table.row_count > 0 else 0
        old_scroll_y = table.scroll_y

        # Clear and rebuild
        table.clear()
        self.service_to_row_key.clear()

        for idx, service in enumerate(self.services):
            status_display = self.format_status(service.status)
            row_key = table.add_row(service.project_name, service.name, status_display)
            self.service_to_row_key[idx] = row_key

        # Restore cursor position and scroll
        if table.row_count > 0:
            # Move cursor without automatic scrolling by first restoring scroll position
            table.move_cursor(row=min(old_cursor, table.row_count - 1))
            # Then force the scroll back to where it was
            table.scroll_y = old_scroll_y

    def format_status(self, status: str) -> str:
        """Format status with color coding"""
        if status == "running":
            return "[green]running[/green]"
        elif status == "stopped":
            return "[red]stopped[/red]"
        elif status == "loading":
            return "[cyan]loading...[/cyan]"
        elif status == "building":
            return "[magenta]building...[/magenta]"
        else:
            return "[yellow]" + status + "[/yellow]"

    async def refresh_services_async(self, services: list[Service]) -> None:
        """Refresh status for a list of services in parallel"""

        # Fetch status for all services in parallel
        async def fetch_status(service: Service) -> tuple[Service, str]:
            status = await self.manager.get_service_status_async(
                service.project_path, service.name
            )
            return service, status

        # Run all status fetches in parallel
        results = await asyncio.gather(*[fetch_status(service) for service in services])

        # Update all service statuses
        for service, status in results:
            service.status = status

        # Rebuild the table after all status updates
        self.rebuild_table()

    async def refresh_project_status_async(self, project_name: str) -> None:
        """Refresh status for all services in a project in parallel"""
        # Get all services for this project
        project_services = [s for s in self.services if s.project_name == project_name]

        # Use the common refresh function
        await self.refresh_services_async(project_services)

    def get_service_row_key(self, service: Service) -> object | None:
        """Get the row key for a service"""
        try:
            service_idx = self.services.index(service)
            return self.service_to_row_key.get(service_idx)
        except ValueError:
            pass
        return None

    async def refresh_all_async(self) -> None:
        """Refresh all services asynchronously in parallel"""
        self.set_status("Refreshing all services...")

        # Set all services to loading status
        for service in self.services:
            service.status = "loading"
        self.rebuild_table()

        # Use the common refresh function
        await self.refresh_services_async(self.services)

        self.set_status("All services refreshed")

    async def refresh_table_async(self) -> None:
        """Refresh the entire service table"""
        await self.refresh_services_async(self.services)

    def get_selected_service(self) -> Service | None:
        """Get the currently selected service"""
        table = self.query_one("#service-table", ServiceList)
        if table.row_count == 0:
            return None

        cursor_row = table.cursor_row
        # Since we rebuild the table in the same order as services list,
        # we can directly use cursor_row as index
        if 0 <= cursor_row < len(self.services):
            return self.services[cursor_row]
        return None

    def set_status(self, message: str) -> None:
        """Update the status bar"""
        status_bar = self.query_one("#status-bar", StatusBar)
        status_bar.message = message

    async def perform_action(self, action: str) -> None:
        """Perform an action on the selected service"""
        service = self.get_selected_service()
        if not service:
            self.set_status("No service selected")
            return

        # Prevent actions on services that are currently loading or building
        if service.status in ("loading", "building"):
            self.set_status(f"Cannot {action} {service.name}: operation in progress")
            return

        project_name = service.project_name

        self.set_status(f"Executing {action} on {project_name}/{service.name}...")

        # Set all services in this project to "loading" status
        project_services = [s for s in self.services if s.project_name == project_name]

        if action == "start":
            # On start, set all services to loading
            for svc in project_services:
                svc.status = "loading"
        else:
            service.status = "loading"

        # Rebuild table to show loading status
        self.rebuild_table()

        # Run action asynchronously
        _success, message = await self.manager.execute_action_async(
            service.project_path, service.name, action
        )

        self.set_status(message)

        if action == "start":
            # On start, refresh all services in the project
            await self.refresh_project_status_async(project_name)
        else:
            # On stop/restart, refresh only the affected service
            await self.refresh_services_async([service])

    def action_start_service(self) -> None:
        """Start the selected service"""
        self.run_worker(self.perform_action("start"))

    def action_stop_service(self) -> None:
        """Stop the selected service"""
        self.run_worker(self.perform_action("stop"))

    def action_restart_service(self) -> None:
        """Restart the selected service"""
        self.run_worker(self.perform_action("restart"))

    def action_build_service(self) -> None:
        """Build the selected service"""
        service = self.get_selected_service()
        if service and service.status in ("loading", "building"):
            self.set_status(f"Cannot build {service.name}: operation in progress")
            return
        self.run_worker(self.perform_build())

    async def perform_build(self) -> None:
        """Build the selected service"""
        service = self.get_selected_service()
        if not service:
            self.set_status("No service selected")
            return

        # Prevent build on services that are currently loading or building
        if service.status in ("loading", "building"):
            self.set_status(f"Cannot build {service.name}: operation in progress")
            return

        self.set_status(f"Building {service.project_name}/{service.name}...")

        # Set service to building status
        service.status = "building"
        self.rebuild_table()

        # Initialize build logs for this service
        service_key = f"{service.project_name}/{service.name}"
        self.build_logs[service_key] = ""

        # Callback to accumulate build logs
        def append_log(line: str) -> None:
            self.build_logs[service_key] += line

        # Run build asynchronously with streaming
        _success, message = await self.manager.build_service_streaming_async(
            service.project_path, service.name, log_callback=append_log
        )

        self.set_status(message)

        # Refresh the service status after build
        await self.refresh_services_async([service])

        # Clean up build logs after a delay
        await asyncio.sleep(5)
        if service_key in self.build_logs:
            del self.build_logs[service_key]

    def action_toggle_service(self) -> None:
        """Toggle the selected service (start if stopped, stop if running)"""
        service = self.get_selected_service()
        if not service:
            self.set_status("No service selected")
            return

        # Prevent toggle on services that are currently loading or building
        if service.status in ("loading", "building"):
            self.set_status(f"Cannot toggle {service.name}: operation in progress")
            return

        # Determine action based on current status
        if service.status == "running":
            self.run_worker(self.perform_action("stop"))
        else:
            self.run_worker(self.perform_action("start"))

    def action_refresh_service_list(self) -> None:
        """Refresh the service list"""
        self.run_worker(self.refresh_all_async())

    async def action_quit(self) -> None:
        """Quit the application, properly cancelling any running workers"""

        # Cancel all running workers and wait for cleanup
        async def cleanup_and_quit() -> None:
            # Cancel all workers
            for worker in self.workers:
                if not worker.is_finished:
                    worker.cancel()

            # Give a short time for cleanup
            await asyncio.sleep(0.1)

            # Now exit
            self.exit()

        self.run_worker(cleanup_and_quit(), exclusive=True)

    def action_open_logs(self) -> None:
        """Show logs for the selected service"""
        service = self.get_selected_service()
        if not service:
            self.set_status("No service selected")
            return

        self.set_status(f"Fetching logs for {service.project_name}/{service.name}...")
        self.run_worker(self.show_logs_async(service))

    async def show_logs_async(self, service: Service) -> None:
        """Fetch and display logs for a service"""
        service_key = f"{service.project_name}/{service.name}"

        # Check if service is currently building and has build logs
        if service.status == "building" and service_key in self.build_logs:
            logs = (
                self.build_logs[service_key] or "Build started, waiting for output..."
            )
            log_type = "build"
        else:
            # Fetch container logs asynchronously
            logs = await self.manager.get_service_logs_async(
                service.project_path,
                service.name,
                200,  # Get last 200 lines
            )
            log_type = "container"

        # Show logs in modal screen with auto-refresh
        self.push_screen(
            LogsScreen(service, logs, self.manager, self.build_logs, log_type)
        )
        self.set_status("Ready")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses"""
        button_id = event.button.id
        if button_id == "btn-start":
            self.action_start_service()
        elif button_id == "btn-stop":
            self.action_stop_service()
        elif button_id == "btn-restart":
            self.action_restart_service()
        elif button_id == "btn-build":
            self.action_build_service()
        elif button_id == "btn-logs":
            self.action_open_logs()
        elif button_id == "btn-refresh":
            self.action_refresh_service_list()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle row selection in the service table (triggered by Enter key)"""
        self.action_toggle_service()


def main() -> None:
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description=(
            "Docker Compose Manager - "
            "A TUI tool to manage multiple docker-compose projects"
        )
    )
    parser.add_argument(
        "root_dir",
        nargs="?",
        default=None,
        help=(
            "Root directory to scan for docker-compose files "
            "(default: current directory)"
        ),
    )

    args = parser.parse_args()

    root_path = Path(args.root_dir).resolve() if args.root_dir else None

    if root_path and not root_path.exists():
        print(f"Error: Directory '{root_path}' does not exist")
        return

    if root_path and not root_path.is_dir():
        print(f"Error: '{root_path}' is not a directory")
        return

    app = DockerComposeManagerApp(root_path=root_path)
    app.run()


if __name__ == "__main__":
    main()
