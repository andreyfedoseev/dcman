#!/usr/bin/env -S uv run
# /// script
# requires-python = ">=3.10"
# dependencies = [
#     "textual>=0.47.0",
#     "pyyaml>=6.0",
# ]
# ///

# TODO: Allow rebuilding 
# TODO: Show logs of a service

"""
Docker Compose Manager - A TUI tool to manage multiple docker-compose projects
"""

import argparse
import asyncio
import subprocess
import yaml
from pathlib import Path
from typing import List, Optional, Tuple
from dataclasses import dataclass

from textual.app import App, ComposeResult
from textual.containers import Vertical, Horizontal
from textual.widgets import Header, Footer, DataTable, Static, Button
from textual.binding import Binding
from textual.reactive import reactive


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
    """Handles Docker Compose operations"""
    
    @staticmethod
    def find_compose_files(root_path: Path) -> List[Path]:
        """Find all docker-compose.yml files in subdirectories, excluding .devcontainer folders"""
        compose_files = []
        for pattern in ["docker-compose.yml", "docker-compose.yaml"]:
            for file in root_path.rglob(pattern):
                # Skip files in .devcontainer folders
                if ".devcontainer" not in file.parts:
                    compose_files.append(file)
        return compose_files
    
    @staticmethod
    def parse_compose_file(compose_file: Path) -> Tuple[str, List[str]]:
        """Parse a docker-compose file and extract service names"""
        try:
            with open(compose_file, 'r') as f:
                compose_data = yaml.safe_load(f)
            
            services = list(compose_data.get('services', {}).keys())
            project_name = compose_file.parent.name
            
            return project_name, services
        except Exception as e:
            print(f"Error parsing {compose_file}: {e}")
            return "", []
    
    @staticmethod
    def get_service_status(project_path: Path, service_name: str) -> str:
        """Get the status of a service"""
        try:
            result = subprocess.run(
                ["docker", "compose", "ps", "-q", service_name],
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=5
            )
            
            if not result.stdout.strip():
                return "stopped"
            
            container_id = result.stdout.strip()
            result = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Status}}", container_id],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            return result.stdout.strip() or "unknown"
        except Exception:
            return "unknown"
    
    @staticmethod
    def execute_action(project_path: Path, service_name: str, action: str) -> Tuple[bool, str]:
        """Execute an action (start/stop/restart) on a service"""
        try:
            if action == "start":
                cmd = ["docker", "compose", "up", "-d", service_name]
            elif action == "stop":
                cmd = ["docker", "compose", "stop", service_name]
            elif action == "restart":
                cmd = ["docker", "compose", "restart", service_name]
            else:
                return False, f"Unknown action: {action}"
            
            result = subprocess.run(
                cmd,
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=60
            )
            
            if result.returncode == 0:
                return True, f"Successfully {action}ed {service_name}"
            else:
                return False, f"Error: {result.stderr}"
        except Exception as e:
            return False, f"Exception: {str(e)}"


class ServiceList(DataTable):
    """Custom DataTable for displaying services"""
    
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cursor_type = "row"
        self.zebra_stripes = True
    
    def on_key(self, event) -> None:
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
    
    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("r", "refresh", "Refresh"),
        Binding("s", "start", "Start"),
        Binding("t", "stop", "Stop"),
        Binding("e", "restart", "Restart"),
    ]
    
    def __init__(self, root_path: Optional[Path] = None):
        super().__init__()
        self.services: List[Service] = []
        self.manager = DockerComposeManager()
        self.root_path = root_path or Path.cwd()
        self.service_to_row_key: dict[int, object] = {}  # Maps service index to row key
    
    def compose(self) -> ComposeResult:
        """Create child widgets"""
        yield Header()
        with Vertical(id="main-container"):
            yield ServiceList(id="service-table")
            with Horizontal(id="button-container"):
                yield Button("Start", variant="success", id="btn-start")
                yield Button("Stop", variant="error", id="btn-stop")
                yield Button("Restart", variant="warning", id="btn-restart")
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
            None,
            self.manager.find_compose_files,
            self.root_path
        )
        
        if not compose_files:
            self.set_status(f"No docker-compose files found in {self.root_path}")
            return
        
        self.set_status(f"Found {len(compose_files)} docker-compose projects, loading services...")
        
        # Process each compose file separately
        for compose_file in compose_files:
            await self.load_project_async(compose_file)
        
        self.set_status(f"Found {len(self.services)} services in {len(compose_files)} projects. Ready.")
    
    async def load_project_async(self, compose_file: Path) -> None:
        """Load a single project's services asynchronously"""
        loop = asyncio.get_event_loop()
        
        # Parse compose file
        project_name, service_names = await loop.run_in_executor(
            None,
            self.manager.parse_compose_file,
            compose_file
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
                status="loading"
            )
            self.services.append(service)
            project_services.append(service)
        
        # Add to table immediately with loading status
        self.add_services_to_table(project_services, start_index)
        
        # Fetch status for each service in background
        await self.refresh_project_status_async(project_name)
    
    def add_services_to_table(self, services: List[Service], start_index: int) -> None:
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
        else:
            return "[yellow]" + status + "[/yellow]"
    
    async def refresh_services_async(self, services: List[Service]) -> None:
        """Refresh status for a list of services in parallel"""
        loop = asyncio.get_event_loop()
        
        # Fetch status for all services in parallel
        async def fetch_status(service: Service) -> Tuple[Service, str]:
            status = await loop.run_in_executor(
                None,
                self.manager.get_service_status,
                service.project_path,
                service.name
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
    
    def get_service_row_key(self, service: Service) -> Optional[object]:
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
    
    def refresh_table(self) -> None:
        """Refresh the entire service table (synchronous version - deprecated, use refresh_all_async)"""
        for service in self.services:
            status = self.manager.get_service_status(service.project_path, service.name)
            service.status = status
        
        # Rebuild table with updated statuses
        self.rebuild_table()
    
    def get_selected_service(self) -> Optional[Service]:
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
        
        # Prevent actions on services that are currently loading
        if service.status == "loading":
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
        
        # Run action in executor to avoid blocking
        loop = asyncio.get_event_loop()
        success, message = await loop.run_in_executor(
            None,
            self.manager.execute_action,
            service.project_path,
            service.name,
            action
        )
        
        self.set_status(message)
        
        if action == "start":
            # On start, refresh all services in the project
            # await asyncio.sleep(0.5)
            await self.refresh_project_status_async(project_name)
        else:
            # On stop/restart, refresh only the affected service
            await self.refresh_services_async([service])
    
    def action_start(self) -> None:
        """Start the selected service"""
        self.run_worker(self.perform_action("start"))
    
    def action_stop(self) -> None:
        """Stop the selected service"""
        self.run_worker(self.perform_action("stop"))
    
    def action_restart(self) -> None:
        """Restart the selected service"""
        self.run_worker(self.perform_action("restart"))
    
    def action_toggle(self) -> None:
        """Toggle the selected service (start if stopped, stop if running)"""
        service = self.get_selected_service()
        if not service:
            self.set_status("No service selected")
            return
        
        # Prevent toggle on services that are currently loading
        if service.status == "loading":
            self.set_status(f"Cannot toggle {service.name}: operation in progress")
            return
        
        # Determine action based on current status
        if service.status == "running":
            self.run_worker(self.perform_action("stop"))
        else:
            self.run_worker(self.perform_action("start"))
    
    def action_refresh(self) -> None:
        """Refresh the service list"""
        self.run_worker(self.refresh_all_async())
    
    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle button presses"""
        button_id = event.button.id
        if button_id == "btn-start":
            self.action_start()
        elif button_id == "btn-stop":
            self.action_stop()
        elif button_id == "btn-restart":
            self.action_restart()
        elif button_id == "btn-refresh":
            self.action_refresh()
    
    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        """Handle row selection in the service table (triggered by Enter key)"""
        self.action_toggle()


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Docker Compose Manager - A TUI tool to manage multiple docker-compose projects"
    )
    parser.add_argument(
        "root_dir",
        nargs="?",
        default=None,
        help="Root directory to scan for docker-compose files (default: current directory)"
    )
    
    args = parser.parse_args()
    
    root_path = Path(args.root_dir).resolve() if args.root_dir else None
    
    if root_path and not root_path.exists():
        print(f"Error: Directory '{root_path}' does not exist")
        return 1
    
    if root_path and not root_path.is_dir():
        print(f"Error: '{root_path}' is not a directory")
        return 1
    
    app = DockerComposeManagerApp(root_path=root_path)
    app.run()


if __name__ == "__main__":
    main() 