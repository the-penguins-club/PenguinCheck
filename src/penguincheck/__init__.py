from .hardware.memory import ram_check
from .hardware.storage import storage_check

def main() -> None:
    print("Hello from penguincheck!")
    print(ram_check())
    print(storage_check())
