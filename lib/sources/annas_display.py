"""Own one Xvfb child; -displayfd atomically selects a free, ready display."""

import asyncio
import shutil


class VirtualDisplay:
    def __init__(self, binary: str = ""):
        self.binary = binary or "Xvfb"
        self.process = None

    async def start(self) -> str:
        executable = shutil.which(self.binary)
        if executable is None:
            raise OSError(f"missing Xvfb binary: {self.binary}")
        try:
            self.process = await asyncio.create_subprocess_exec(
                executable,
                "-displayfd",
                "1",
                "-screen",
                "0",
                "1920x1080x24",
                "-nolisten",
                "tcp",
                "-extension",
                "GLX",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            ready = await asyncio.wait_for(self.process.stdout.readline(), timeout=5)
            if not ready.strip().isdigit() or self.process.returncode is not None:
                raise OSError("Xvfb did not report a ready display")
            return ":" + ready.decode("ascii").strip()
        except BaseException:
            await self.close()
            raise

    async def close(self):
        process, self.process = self.process, None
        if process is None or process.returncode is not None:
            return
        try:
            process.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(process.wait(), timeout=0.25)
        except asyncio.TimeoutError:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
