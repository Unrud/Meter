import asyncio
import network
import sys
import time
from machine import Pin, UART, WDT

# https://github.com/miguelgrinberg/microdot
from microdot import Microdot, Response

import config
from locale import get_translation


DATA_BUF_SIZE = 8192
DATA_TIMEOUT_MS = 100
MAX_DATA_AGE_MS = 10_000


__wdt = WDT()
__wdt_monitors = []

network.country(config.WIFI_COUNTRY)
network.hostname(config.HOSTNAME)
__nic = network.WLAN(network.STA_IF)

# MessageT = list[None | bytes | bool | int | 'MessageT']
__message = None  # type: 'MessageT' | None
__buf = memoryview(bytearray(DATA_BUF_SIZE))
__buf_len = 0

__active_energy_import = None
__active_energy_feed = None
__active_power = None
__active_power_history = []
__last_update_ticks_ms = None


async def watchdog_task():
    good_times = {}
    while True:
        now = time.ticks_ms()
        good = True
        for monitor_func, timeout in __wdt_monitors:
            good_time = good_times.get(id(monitor_func), now)
            if not monitor_func():
                good_time = now
            elif time.ticks_diff(now, good_time) >= timeout:
                good = False
            good_times[id(monitor_func)] = good_time
        if good:
            __wdt.feed()
        await asyncio.sleep(1)


async def wifi_task():
    # Reconnect WIFI to renew DHCP lease
    while True:
        __nic.active(True)
        __nic.connect(config.WIFI_SSID, config.WIFI_PASSWORD)
        await asyncio.sleep(60 * 60)
        __nic.disconnect()
        __nic.active(False)
        __nic.deinit()


def check_connection():
    return (
        __last_update_ticks_ms is not None
        and time.ticks_diff(time.ticks_ms(), __last_update_ticks_ms) <= MAX_DATA_AGE_MS
    )


def crc16_x25(data):
    """https://stackoverflow.com/a/58770517"""
    w_crc = 0xFFFF
    for c in data:
        w_crc ^= c
        for _ in range(8):
            w_crc = (w_crc >> 1) ^ 0x8408 if w_crc & 0x0001 else w_crc >> 1
    return w_crc ^ 0xFFFF


def decode_sml(buf):
    i = 0
    message_stack = [[i, -1, []]]

    def can_read():
        nonlocal i
        return max(0, len(buf) - i)

    def write_message(message):
        nonlocal message_stack
        _, cur_message_l, cur_message = message_stack[-1]
        if cur_message_l != -1:
            message_stack[-1][1] -= 1
            while message_stack[-1][1] == 0:
                message_stack.pop()
        cur_message.append(message)
        return message

    # Escape Sequence
    for _ in range(4):
        if not can_read() or buf[i] != 0x1B:
            return None
        i += 1
    # Start Version 1
    for _ in range(4):
        if not can_read() or buf[i] != 0x01:
            return None
        i += 1
    # Check global crc
    if can_read() < 2 or crc16_x25(buf[:-2]) != (buf[-1] << 8) + buf[-2]:
        return None
    while True:
        if not can_read():
            return None
        if buf[i] == 0x1B or buf[i] == 0x00 and len(message_stack) == 1:
            if len(message_stack) != 1:
                return None
            break
        if buf[i] == 0x00:
            i += 1
            write_message(None)
            continue
        c = (buf[i] >> 4) & 0b111
        l = buf[i] & 0xF
        c_len = 1
        if buf[i] >> 7:
            # long len
            i += 1
            if not can_read():
                return None
            l = (l << 8) + buf[i]
            c_len += 1
        i += 1
        if l < 0:
            return None
        if c == 0x0 or c == 0x4 or c == 0x5 or c == 0x6:
            # data
            l -= c_len
            if can_read() < l:
                return None
            data = buf[i : i + l]
            i += l
            if c == 0x0:
                # octet string
                write_message(bytes(data))
                continue
            n = 0
            for d in data:
                # big endian
                n = (n << 8) + d
            if c == 0x4:
                # boolean
                write_message(n != 0)
                continue
            # integer
            if c == 0x5 and data and data[0] >> 7:
                # signed negative integer
                n ^= (1 << (len(data) * 8)) - 1
                n += 1
                n = -n
            write_message(n)
            continue
        if c == 0x7:
            # list
            if l > 0:
                message_stack.append([i - c_len, l, write_message([])])
            continue
        return None
    padding = 0
    while can_read() and buf[i] == 0x00:
        i += 1
        padding += 1
    # Escape Sequence
    for _ in range(4):
        if not can_read() or buf[i] != 0x1B:
            return None
        i += 1
    if not can_read() or buf[i] != 0x1A:
        return None
    i += 1
    # Padding
    if not can_read() or buf[i] != padding:
        return None
    i += 1
    if can_read() != 2:
        return None
    return message_stack[0][2]


def flip_buffer():
    global __message, __buf, __buf_len, __active_energy_import
    global __active_energy_feed, __active_power, __active_power_history
    global __last_update_ticks_ms
    now_ticks_ms = time.ticks_ms()
    message = decode_sml(__buf[:__buf_len])
    __buf_len = 0
    if message is None:
        return
    __message = message
    __last_update_ticks_ms = now_ticks_ms
    __active_energy_import = None
    __active_energy_feed = None
    __active_power = None
    for msg in message:
        try:
            if msg[3][0] != 1793:
                continue
            for entry in msg[3][1][4]:
                if entry[0] == b"\x01\x00\x01\x08\x00\xff":
                    __active_energy_import = int(entry[5] * 10 ** entry[4])
                if entry[0] == b"\x01\x00\x02\x08\x00\xff":
                    __active_energy_feed = int(entry[5] * 10 ** entry[4])
                if entry[0] == b"\x01\x00\x10\x07\x00\xff":
                    __active_power = int(entry[5] * 10 ** entry[4])
        except (IndexError, TypeError, ValueError) as e:
            sys.print_exception(e)
    if __active_power is not None:
        __active_power_history.append((now_ticks_ms, __active_power))
    while (
        __active_power_history
        and time.ticks_diff(now_ticks_ms, __active_power_history[0][0])
        > config.HISTORY_MS
    ):
        del __active_power_history[0]


async def uart_task():
    global __buf, __buf_len
    uart0 = UART(
        0,
        baudrate=9600,
        bits=8,
        parity=None,
        stop=1,
        tx=Pin(0),
        rx=Pin(1),
        invert=UART.INV_TX,
    )
    last_ticks_ms = time.ticks_ms()
    while True:
        available = uart0.any()
        if not available:
            if (
                time.ticks_diff(time.ticks_ms(), last_ticks_ms) > DATA_TIMEOUT_MS
                and __buf_len
            ):
                flip_buffer()
            await asyncio.sleep_ms(5)
            continue
        last_ticks_ms = time.ticks_ms()
        read_len = min(available, len(__buf) - __buf_len)
        if read_len:
            uart0.readinto(__buf[__buf_len:], read_len)
        if read_len < available:
            uart0.read(available - read_len)
        __buf_len += read_len


app = Microdot()


@app.errorhandler(MemoryError)
async def memory_error(request, exception):
    request.app.shutdown()
    return "Out Of Memory", 500


def q(s):
    """Quote HTML"""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


@app.get("/")
def index(request):
    async def stream(t):
        yield "<!doctype html>"
        yield f'<html lang="{q(t.lang)}">'
        yield '<meta charset="utf-8">'
        yield (
            '<meta content="width=device-width, initial-scale=1" ' 'name="viewport">'
        )
        yield f'<title>{q(t("Electricity meter"))}</title>'
        yield "<style>"
        yield ":root {"
        yield "color-scheme:light dark;"
        yield "}"
        yield ".error {"
        yield "background:Canvas;"
        yield "color:red;"
        yield "position:sticky;"
        yield "top:0;"
        yield "}"
        yield "</style>"
        if config.REFRESH_WEBPAGE:
            yield '<style onload="' + q(
                f"const refreshInterval = {config.REFRESH_WEBPAGE * 1000:.0f};"
                + 'const errorElement = document.getElementById("error");'
                + "let startTime = Date.now();"
                + "let timeoutId = 0;"
                + "async function update() {"
                + "window.clearTimeout(timeoutId);"
                + 'document.removeEventListener("visibilitychange", update);'
                + "if (document.hidden) {"
                + 'console.debug("Update: document hidden");'
                + 'document.addEventListener("visibilitychange", update);'
                + "return;"
                + "}"
                + "const elapsed = Date.now() - startTime;"
                + "if (elapsed >= 0 && elapsed < refreshInterval) {"
                + "const wait = refreshInterval - elapsed;"
                + 'console.debug("Update: waiting", wait);'
                + "timeoutId = window.setTimeout(update, wait);"
                + 'document.addEventListener("visibilitychange", update);'
                + "return;"
                + "}"
                + "if (elapsed < 0 || elapsed >= refreshInterval*2) {"
                + 'console.debug("Update: stale");'
                + 'if (errorElement) errorElement.style.removeProperty("display");'
                + "}"
                + 'console.debug("Update: fetch");'
                + "try {"
                + "const resp = await fetch(location.href);"
                + "if(!resp.ok){"
                + "throw new Error(`Response status: ${resp.status}`);"
                + "}"
                + 'console.debug("Update: success");'
                + "document.documentElement.innerHTML = await resp.text();"
                + "} catch (err) {"
                + 'console.error("Update: error", err);'
                + 'if (errorElement) errorElement.style.removeProperty("display");'
                + "startTime = Date.now();"
                + "update();"
                + "}"
                + "}"
                + "update();"
            ) + '"></style>'
        yield f'<h1>{q(t("Electricity meter"))}</h1>'
        yield '<h2 id="error" class="error"'
        if check_connection():
            yield ' style="display:none"'
        yield f'>{q(t("No connection"))}</h2>'
        yield f'<h2>{q(t("Active energy import"))}</h2>'
        yield f'<p>{q(t.number(__active_energy_import, "Wh"))}</p>'
        yield f'<h2>{q(t("Active energy feed"))}</h2>'
        yield f'<p>{q(t.number(__active_energy_feed, "Wh"))}</p>'
        yield f'<h2>{q(t("Active power"))}</h2>'
        yield f'<p>{q(t.number(__active_power, "W"))}</p>'

    return Response(
        body=stream(get_translation(request)),
        status_code=200,
        headers={"Content-Type": "text/html; charset=utf-8"},
    )


@app.get("/data")
def data(request):
    if not check_connection():
        return "No Data", 503
    return {
        "activeEnergyImport": __active_energy_import,
        "activeEnergyFeed": __active_energy_feed,
        "activePower": __active_power,
        "activePowerAvg": (
            round(
                sum(map(lambda e: e[1], __active_power_history))
                / len(__active_power_history)
            )
            if __active_power_history
            else None
        ),
        "activePowerMin": (
            min(map(lambda e: e[1], __active_power_history))
            if __active_power_history
            else None
        ),
        "activePowerMax": (
            max(map(lambda e: e[1], __active_power_history))
            if __active_power_history
            else None
        ),
    }


@app.get("/raw-data")
def raw_data(request):
    async def stream():
        message_stack = [[0, __message]]
        prev_depth = None
        while message_stack:
            depth, message = message_stack.pop()
            if prev_depth is not None:
                if depth == prev_depth:
                    yield ","
                yield "\n"
            prev_depth = depth
            yield "    " * depth
            if isinstance(message, list):
                yield "["
                message_stack.append([depth, "]"])
                message_stack.extend([depth + 1, m] for m in reversed(message))
            else:
                if isinstance(message, bytes):
                    yield '"'
                    for i, c in enumerate(message):
                        if i > 0:
                            yield " "
                        yield f"{c:02X}"
                    yield '"'
                elif message is None:
                    yield "null"
                elif message is True:
                    yield "true"
                elif message is False:
                    yield "false"
                else:
                    yield str(message)
        yield "\n"

    return Response(
        body=stream(),
        status_code=200,
        headers={"Content-Type": "application/json"},
    )


__wdt_monitors.extend(
    [
        (asyncio.create_task(uart_task()).done, 0),
        (asyncio.create_task(watchdog_task()).done, 0),
        (asyncio.create_task(wifi_task()).done, 0),
        (lambda: not __nic.isconnected(), 600_000),
        (lambda: not check_connection(), 600_000),
    ]
)
app.run(port=80)
