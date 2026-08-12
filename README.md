# FRIDAY MK1

A modular AI desktop assistant built from scratch using Python, Electron, JavaScript, and modern AI APIs. Designed to explore voice interaction, computer vision, persistent memory, intelligent planning, and desktop automation.

---

## Features

- Voice controlled AI assistant
- Natural language conversations
- Persistent memory
- Multi-step planning
- Desktop workstation
- Dynamic widgets
- Weather
- News
- Calendar integration
- Interactive map
- Tool execution
- Computer vision framework
- Modular architecture

---

## Example Commands

FRIDAY understands natural language commands.

### System

- "FRIDAY"
- "Wake up"
- "Go to sleep"
- "Open workstation"
- "Close workstation"

### Widgets

- "Show weather"
- "Show news"
- "Show map"
- "Open calendar"
- "Clear workstation"

### Productivity

- "Remember that..."
- "What do you remember about me?"
- "Plan my day"
- "Open settings"

### General Questions

- "What's the weather today?"
- "Summarize today's news."
- "What time is it?"

---

## Project Structure

```
FRIDAY_OS/
├── Core_Cognition/
├── Sensory_Array/
├── Visual_Interface/
├── Data/
└── Tests/
```

---

## Installation

### Clone

```bash
git clone https://github.com/<username>/FRIDAY-MK1.git
cd FRIDAY-MK1
```

### Create virtual environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### Install dependencies

```bash
pip install -r requirements.txt
```

---

## Configuration

Copy the template

```bash
cp FRIDAY_OS/Core_Cognition/.env.example FRIDAY_OS/Core_Cognition/.env
```

Fill in

- GEMINI_API_KEY
- FISH_API_KEY
- VOICE_ID

Google Calendar requires

- calendar_credentials.json
- calendar_token.json

These files are intentionally excluded from the repository.

---

## Running FRIDAY

Run the backend

```bash
./run_friday.sh
```

Run the Electron interface

```bash
npm start
```

---

## Technologies

- Python
- Electron
- JavaScript
- HTML/CSS
- Google Gemini
- Fish Audio
- OpenCV

---

## Roadmap

Current version (MK1)

- Voice assistant
- Memory
- Desktop workstation
- Calendar
- Widgets
- Planning

Future versions

- Better reasoning
- Improved computer vision
- Smarter planning
- Robotics integration
- Embedded hardware

---

## About

FRIDAY MK1 is my first major software engineering project.

It was built as a learning project while studying Intelligent Systems Engineering at DePaul University and serves as the foundation for future AI, robotics, and autonomous systems developed under Meholli Industries.
