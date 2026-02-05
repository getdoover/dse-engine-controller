# Doover Application Template

This repository serves as a template for creating Doover applications.

It provides a structured layout for application code, deployment configurations, simulators, and tests. The template is
designed to simplify the development and deployment of Doover-compatible applications.

The basic structure of the repository is as follows:

## Getting Started

```
README.md           <-- App description
DEVELOPMENT.md      <-- This file
pyproject.toml      <-- Python project configuration file (including dependencies)
Dockerfile          <-- Dockerfile for building the application image
doover_config.json  <-- Configuration file for doover

src/app_template/   <-- Application directory
  application.py    <-- Main application code
  app_config.py     <-- Config schema definition
  app_ui.py         <-- UI code (if applicable)
  app_state.py      <-- State machine (if applicable)

simulator/
  app_config.json   <-- Sample configuration for the simulator
  docker-compose.yml <-- Docker Compose file for the simulator
  
tests/
    test_imports.py  <-- Test file for the application
```

The `doover_config.json` file is the doover configuration file for the application. 

It defines all metadata about the application, including name, short and long description, 
dependent apps, image name, owner organisation, container registry and more.

### Prerequisites

- Docker and Docker Compose installed
- Python 3.11 or later (if running locally)
- Pipenv for managing Python dependencies

### Running Locally

1. Run the application:

```bash
doover app run
```

## Simulators

The `simulator/` directory contains tools for simulating application behavior. For example:

- `app_config.json`: Sample configuration file for the app.
- `docker-compose.yml`: Defines services for running the application.

You can find a sample simulator in the `simulator/sample/` directory. While it is fairly bare-bones, it shows
positioning of the simulator in the application structure, and how to start the simulator alongside your application.

## Testing

Run the tests using the following command:

```bash
pytest tests/
```

## Deployment

The `deployment/` directory contains deployment configurations, including a `docker-compose.yml` file for orchestrating
services.

## Customization

To create your own Doover application:

1. Modify the application logic in the appropriate directory.
2. Update the simulator and test configurations as needed.
3. Adjust deployment configurations to suit your requirements.
