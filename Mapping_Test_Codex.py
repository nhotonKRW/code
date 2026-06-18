# -*- coding: utf-8 -*-

# Projection Mapping Tool
# - 1번/메인 모니터 화면을 캡처한다.
# - 2번 모니터/빔프로젝터 화면에 그리드 기반으로 변형해서 출력한다.
# - 설정 모드에서는 가장자리 제어점만 움직이고 내부 점은 자동 계산한다.
# - 인터랙션 모드에서는 출력 화면의 마우스/키보드 입력을 원본 화면으로 전달한다.

import cv2
import numpy as np
import json
import os
import win32api
import win32gui
import win32ui
import win32con


# 설정 파일은 실행 위치가 아니라 이 py 파일이 있는 폴더 기준으로 저장한다.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "mapping_config_codex.json")
LEGACY_CONFIG_FILE = os.path.join(BASE_DIR, "mapping_config.json")

# 그리드 셀 개수다. 실제 점 개수는 (GRID_ROWS + 1) x (GRID_COLS + 1).
# 현재 코드는 외곽 점만 사용자가 직접 편집하고 내부 점은 자동 보간한다.
# 그리드를 더 촘촘하게 보이도록 8 x 8 셀로 사용한다.
# 값이 커질수록 곡면 표현은 부드러워지지만 셀별 warp 연산이 늘어 FPS는 낮아질 수 있다.
GRID_ROWS = 8
GRID_COLS = 8
POINT_PICK_RADIUS = 18
LOAD_LEGACY_CONFIG_ON_FIRST_RUN = False

# 실행 중 계속 바뀌는 상태값들.
src_grid = None
dst_grid = None
selected_point = None
is_setup_mode = True
is_fullscreen = True
source_monitor_info = None
target_input_hwnd = None
last_target_hwnd = None

# OpenCV waitKeyEx는 방향키/F키 같은 특수키를 일반 ASCII와 다르게 반환한다.
# Windows 가상 키 코드로 다시 변환하기 위한 테이블이다.
KEY_F8_CODES = {win32con.VK_F8, win32con.VK_F8 << 16}
SPECIAL_KEY_TO_VK = {
    win32con.VK_LEFT << 16: win32con.VK_LEFT,
    win32con.VK_UP << 16: win32con.VK_UP,
    win32con.VK_RIGHT << 16: win32con.VK_RIGHT,
    win32con.VK_DOWN << 16: win32con.VK_DOWN,
    win32con.VK_HOME << 16: win32con.VK_HOME,
    win32con.VK_END << 16: win32con.VK_END,
    win32con.VK_PRIOR << 16: win32con.VK_PRIOR,
    win32con.VK_NEXT << 16: win32con.VK_NEXT,
    win32con.VK_INSERT << 16: win32con.VK_INSERT,
    win32con.VK_DELETE << 16: win32con.VK_DELETE,
}
for _vk in range(win32con.VK_F1, win32con.VK_F12 + 1):
    SPECIAL_KEY_TO_VK[_vk << 16] = _vk

BASIC_KEY_TO_VK = {
    8: win32con.VK_BACK,
    9: win32con.VK_TAB,
    13: win32con.VK_RETURN,
    27: win32con.VK_ESCAPE,
    32: win32con.VK_SPACE,
}


def create_grid_from_quad(quad, rows, cols):
    # 네 모서리 좌표를 기준으로 균등한 그리드를 만든다.
    # 기존 사각형 매핑 설정(dst_pts)을 새 그리드 방식으로 변환할 때도 사용한다.
    quad = np.asarray(quad, dtype=np.float32)
    top_left, top_right, bottom_right, bottom_left = quad
    grid = np.zeros((rows + 1, cols + 1, 2), dtype=np.float32)
    for row in range(rows + 1):
        v = row / rows
        left = (1.0 - v) * top_left + v * bottom_left
        right = (1.0 - v) * top_right + v * bottom_right
        for col in range(cols + 1):
            u = col / cols
            grid[row, col] = (1.0 - u) * left + u * right
    return grid


def create_default_grid(width, height):
    quad = np.array(
        [[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]],
        dtype=np.float32,
    )
    return create_grid_from_quad(quad, GRID_ROWS, GRID_COLS)


def is_edge_point(row, col):
    # 사용자가 직접 잡을 수 있는 점은 외곽선 위의 점으로 제한한다.
    return row == 0 or row == GRID_ROWS or col == 0 or col == GRID_COLS


def rebuild_auto_inner_points(grid):
    # 외곽선 점이 움직이면 내부 점을 Coons patch 방식으로 다시 계산한다.
    # 덕분에 내부 점을 직접 편집하지 않아도 외곽 형태를 따라 자연스럽게 휘어진다.
    grid = np.asarray(grid, dtype=np.float32).copy()
    top_left = grid[0, 0]
    top_right = grid[0, GRID_COLS]
    bottom_right = grid[GRID_ROWS, GRID_COLS]
    bottom_left = grid[GRID_ROWS, 0]

    for row in range(1, GRID_ROWS):
        v = row / GRID_ROWS
        left = grid[row, 0]
        right = grid[row, GRID_COLS]
        for col in range(1, GRID_COLS):
            u = col / GRID_COLS
            top = grid[0, col]
            bottom = grid[GRID_ROWS, col]
            edge_blend = (1.0 - u) * left + u * right + (1.0 - v) * top + v * bottom
            corner_blend = (
                (1.0 - u) * (1.0 - v) * top_left
                + u * (1.0 - v) * top_right
                + u * v * bottom_right
                + (1.0 - u) * v * bottom_left
            )
            grid[row, col] = edge_blend - corner_blend
    return grid


def sample_edge_points(points, count):
    # 기존 설정 파일의 외곽선을 현재 그리드 개수에 맞춰 다시 샘플링한다.
    points = np.asarray(points, dtype=np.float32)
    if len(points) == 1:
        return np.repeat(points, count, axis=0)

    sampled = np.zeros((count, 2), dtype=np.float32)
    for index in range(count):
        t = index / (count - 1) if count > 1 else 0.0
        pos = t * (len(points) - 1)
        left_index = int(np.floor(pos))
        right_index = min(left_index + 1, len(points) - 1)
        local_t = pos - left_index
        sampled[index] = (1.0 - local_t) * points[left_index] + local_t * points[right_index]
    return sampled


def resample_grid_edges_to_current(grid):
    # 예전 4 x 4 같은 다른 해상도의 저장 그리드를 현재 GRID_ROWS/COLS로 변환한다.
    grid = np.asarray(grid, dtype=np.float32)
    new_grid = np.zeros((GRID_ROWS + 1, GRID_COLS + 1, 2), dtype=np.float32)

    new_grid[0, :] = sample_edge_points(grid[0, :], GRID_COLS + 1)
    new_grid[GRID_ROWS, :] = sample_edge_points(grid[-1, :], GRID_COLS + 1)
    new_grid[:, 0] = sample_edge_points(grid[:, 0], GRID_ROWS + 1)
    new_grid[:, GRID_COLS] = sample_edge_points(grid[:, -1], GRID_ROWS + 1)
    return rebuild_auto_inner_points(new_grid)


def load_grid_file(path, output_width, output_height):
    # 새 grid_pts 설정과 예전 dst_pts 네 점 설정을 모두 읽을 수 있게 유지한다.
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    if "grid_pts" in data:
        loaded_grid = np.array(data["grid_pts"], dtype=np.float32)
        expected_shape = (GRID_ROWS + 1, GRID_COLS + 1, 2)
        if loaded_grid.shape == expected_shape:
            print(f"Loaded grid config: {path}")
            return rebuild_auto_inner_points(loaded_grid)
        if loaded_grid.ndim == 3 and loaded_grid.shape[2] == 2:
            print(f"Resampled saved grid config to {GRID_ROWS}x{GRID_COLS}: {path}")
            return resample_grid_edges_to_current(loaded_grid)
        print("Saved grid size does not match current GRID_ROWS/GRID_COLS.")

    if "dst_pts" in data:
        quad = np.array(data["dst_pts"], dtype=np.float32)
        if quad.shape == (4, 2):
            print(f"Converted old four-corner config to editable edge grid: {path}")
            return rebuild_auto_inner_points(create_grid_from_quad(quad, GRID_ROWS, GRID_COLS))

    return create_default_grid(output_width, output_height)


def load_config(output_width, output_height):
    global dst_grid
    config_paths = [CONFIG_FILE]
    if LOAD_LEGACY_CONFIG_ON_FIRST_RUN:
        config_paths.append(LEGACY_CONFIG_FILE)

    for path in config_paths:
        if not os.path.exists(path):
            continue
        try:
            dst_grid = load_grid_file(path, output_width, output_height)
            return
        except Exception as exc:
            print(f"Failed to load config {path}. Error: {exc}")

    dst_grid = create_default_grid(output_width, output_height)
    print("Using default full-screen grid. Press L to import the old four-corner config if needed.")


def load_legacy_config(output_width, output_height):
    if not os.path.exists(LEGACY_CONFIG_FILE):
        print(f"Legacy config was not found: {LEGACY_CONFIG_FILE}")
        return None
    try:
        return load_grid_file(LEGACY_CONFIG_FILE, output_width, output_height)
    except Exception as exc:
        print(f"Failed to load legacy config. Error: {exc}")
        return None


def save_config():
    # 현재 그리드 전체를 저장한다. 다음 실행 시 내부 점은 외곽 기준으로 다시 보정된다.
    data = {
        "grid_rows": GRID_ROWS,
        "grid_cols": GRID_COLS,
        "editable": "edge_points_only",
        "grid_pts": dst_grid.tolist(),
    }
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"Saved grid config: {CONFIG_FILE}")


def get_monitors():
    # Windows에 연결된 모니터의 위치와 크기를 가져온다.
    # 다중 모니터에서는 좌표가 음수일 수도 있으므로 left/top도 함께 저장한다.
    monitors = []
    for handle, _hdc, _rect in win32api.EnumDisplayMonitors():
        info = win32api.GetMonitorInfo(handle)
        left, top, right, bottom = info["Monitor"]
        monitors.append(
            {
                "left": left,
                "top": top,
                "right": right,
                "bottom": bottom,
                "width": right - left,
                "height": bottom - top,
                "primary": bool(info.get("Flags", 0) & 1),
                "device": info.get("Device", ""),
            }
        )
    return monitors


def choose_source_and_output_monitors():
    # 기본 정책: 메인 모니터를 캡처 원본으로, 비메인 모니터를 출력 대상으로 사용한다.
    monitors = get_monitors()
    if not monitors:
        raise RuntimeError("No monitor was found.")
    source_monitor = next((m for m in monitors if m["primary"]), monitors[0])
    output_monitor = next((m for m in monitors if not m["primary"]), source_monitor)
    return source_monitor, output_monitor, monitors


def capture_screen_region(left, top, width, height):
    # Win32 GDI BitBlt로 화면 일부를 빠르게 캡처한다.
    # OpenCV는 BGR을 쓰므로 BGRA 캡처 결과에서 알파 채널을 제거한다.
    screen_dc = None
    dc_obj = None
    compatible_dc = None
    data_bitmap = None
    try:
        screen_dc = win32gui.GetDC(0)
        dc_obj = win32ui.CreateDCFromHandle(screen_dc)
        compatible_dc = dc_obj.CreateCompatibleDC()
        data_bitmap = win32ui.CreateBitmap()
        data_bitmap.CreateCompatibleBitmap(dc_obj, width, height)
        compatible_dc.SelectObject(data_bitmap)
        compatible_dc.BitBlt((0, 0), (width, height), dc_obj, (left, top), win32con.SRCCOPY)
        bitmap_bits = data_bitmap.GetBitmapBits(True)
        img = np.frombuffer(bitmap_bits, dtype=np.uint8)
        img.shape = (height, width, 4)
        return cv2.cvtColor(img, cv2.COLOR_BGRA2BGR)
    finally:
        if data_bitmap is not None:
            win32gui.DeleteObject(data_bitmap.GetHandle())
        if compatible_dc is not None:
            compatible_dc.DeleteDC()
        if dc_obj is not None:
            dc_obj.DeleteDC()
        if screen_dc is not None:
            win32gui.ReleaseDC(0, screen_dc)


def make_lparam(x, y):
    # Windows 메시지의 lParam은 y 좌표와 x 좌표를 16비트씩 합쳐서 만든다.
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


def window_from_screen_point(x, y):
    hwnd = win32gui.WindowFromPoint((int(x), int(y)))
    return hwnd if hwnd else None


def get_fallback_input_hwnd():
    if source_monitor_info is None:
        return None
    x = source_monitor_info["left"] + source_monitor_info["width"] // 2
    y = source_monitor_info["top"] + source_monitor_info["height"] // 2
    return window_from_screen_point(x, y)


def get_keyboard_target_hwnd():
    # 키보드는 마지막으로 마우스 이벤트가 전달된 창으로 보낸다.
    # 아직 대상 창이 없으면 원본 모니터 중앙에 있는 창을 임시 대상으로 잡는다.
    global target_input_hwnd
    if target_input_hwnd is not None and win32gui.IsWindow(target_input_hwnd):
        return target_input_hwnd
    target_input_hwnd = get_fallback_input_hwnd()
    return target_input_hwnd


def output_to_source_point(x, y, source_grid, target_grid):
    # 출력 화면 좌표가 어떤 그리드 셀 안에 있는지 찾고,
    # 해당 셀의 역원근변환으로 원본 화면 좌표를 계산한다.
    target_point = (float(x), float(y))
    for row in range(GRID_ROWS):
        for col in range(GRID_COLS):
            src_quad = np.array(
                [source_grid[row, col], source_grid[row, col + 1], source_grid[row + 1, col + 1], source_grid[row + 1, col]],
                dtype=np.float32,
            )
            dst_quad = np.array(
                [target_grid[row, col], target_grid[row, col + 1], target_grid[row + 1, col + 1], target_grid[row + 1, col]],
                dtype=np.float32,
            )
            if cv2.pointPolygonTest(dst_quad, target_point, False) < 0:
                continue
            matrix = cv2.getPerspectiveTransform(dst_quad, src_quad)
            mapped = cv2.perspectiveTransform(np.array([[[float(x), float(y)]]], dtype=np.float32), matrix)
            return mapped[0, 0]
    return None


def map_output_to_source_screen(x, y):
    # 출력 창 내부 좌표를 Windows 전체 화면 기준의 원본 모니터 좌표로 바꾼다.
    if source_monitor_info is None or src_grid is None or dst_grid is None:
        return None
    source_point = output_to_source_point(x, y, src_grid, dst_grid)
    if source_point is None:
        return None
    source_x = int(np.clip(source_point[0], 0, source_monitor_info["width"] - 1))
    source_y = int(np.clip(source_point[1], 0, source_monitor_info["height"] - 1))
    return source_monitor_info["left"] + source_x, source_monitor_info["top"] + source_y


def post_mouse_message(message, screen_x, screen_y, wparam=0, hwnd=None):
    # 역매핑된 원본 화면 좌표 아래의 실제 창에 마우스 메시지를 보낸다.
    global target_input_hwnd, last_target_hwnd
    if hwnd is None:
        hwnd = window_from_screen_point(screen_x, screen_y)
    if hwnd is None:
        return
    target_input_hwnd = hwnd
    if hwnd != last_target_hwnd:
        try:
            print(f"Input target: hwnd={hwnd}, title='{win32gui.GetWindowText(hwnd)}'")
        except Exception:
            print(f"Input target: hwnd={hwnd}")
        last_target_hwnd = hwnd
    client_x, client_y = win32gui.ScreenToClient(hwnd, (int(screen_x), int(screen_y)))
    win32api.PostMessage(hwnd, message, wparam, make_lparam(client_x, client_y))


def get_mouse_wheel_delta(flags):
    # OpenCV 마우스 휠 값은 flags 상위 16비트에 들어온다.
    delta = (flags >> 16) & 0xFFFF
    if delta & 0x8000:
        delta -= 0x10000
    return delta


def post_mouse_wheel(screen_x, screen_y, delta):
    hwnd = window_from_screen_point(screen_x, screen_y)
    if hwnd is None:
        return
    wparam = (int(delta) & 0xFFFF) << 16
    win32api.PostMessage(hwnd, win32con.WM_MOUSEWHEEL, wparam, make_lparam(screen_x, screen_y))


def handle_forwarded_mouse_event(event, x, y, flags):
    # 인터랙션 모드에서 호출된다.
    # 프로젝터 출력 화면에서 발생한 마우스 이벤트를 원본 화면 좌표로 바꿔 전달한다.
    mapped = map_output_to_source_screen(x, y)
    if mapped is None:
        return
    screen_x, screen_y = mapped
    if event == cv2.EVENT_MOUSEMOVE:
        state = 0
        if flags & cv2.EVENT_FLAG_LBUTTON:
            state |= win32con.MK_LBUTTON
        if flags & cv2.EVENT_FLAG_RBUTTON:
            state |= win32con.MK_RBUTTON
        if flags & cv2.EVENT_FLAG_MBUTTON:
            state |= win32con.MK_MBUTTON
        post_mouse_message(win32con.WM_MOUSEMOVE, screen_x, screen_y, state)
    elif event == cv2.EVENT_LBUTTONDOWN:
        post_mouse_message(win32con.WM_LBUTTONDOWN, screen_x, screen_y, win32con.MK_LBUTTON)
    elif event == cv2.EVENT_LBUTTONUP:
        post_mouse_message(win32con.WM_LBUTTONUP, screen_x, screen_y, 0)
    elif event == cv2.EVENT_RBUTTONDOWN:
        post_mouse_message(win32con.WM_RBUTTONDOWN, screen_x, screen_y, win32con.MK_RBUTTON)
    elif event == cv2.EVENT_RBUTTONUP:
        post_mouse_message(win32con.WM_RBUTTONUP, screen_x, screen_y, 0)
    elif event == cv2.EVENT_MBUTTONDOWN:
        post_mouse_message(win32con.WM_MBUTTONDOWN, screen_x, screen_y, win32con.MK_MBUTTON)
    elif event == cv2.EVENT_MBUTTONUP:
        post_mouse_message(win32con.WM_MBUTTONUP, screen_x, screen_y, 0)
    elif event == cv2.EVENT_MOUSEWHEEL:
        post_mouse_wheel(screen_x, screen_y, get_mouse_wheel_delta(flags))


def mouse_callback(event, x, y, flags, param):
    # 설정 모드: 외곽 제어점 편집
    # 인터랙션 모드: 마우스 이벤트를 원본 화면으로 전달
    global dst_grid, selected_point
    if not is_setup_mode:
        handle_forwarded_mouse_event(event, x, y, flags)
        return

    if event == cv2.EVENT_LBUTTONDOWN:
        mouse_pos = np.array([x, y], dtype=np.float32)
        distances = np.linalg.norm(dst_grid - mouse_pos, axis=2)
        for row in range(1, GRID_ROWS):
            for col in range(1, GRID_COLS):
                distances[row, col] = np.inf
        row, col = np.unravel_index(np.argmin(distances), distances.shape)
        if is_edge_point(row, col) and distances[row, col] <= POINT_PICK_RADIUS:
            selected_point = (row, col)
    elif event == cv2.EVENT_MOUSEMOVE and selected_point is not None:
        row, col = selected_point
        dst_grid[row, col] = [x, y]
        dst_grid = rebuild_auto_inner_points(dst_grid)
    elif event == cv2.EVENT_LBUTTONUP:
        selected_point = None


def is_valid_quad(quad):
    # 뒤집히거나 너무 작은 셀은 원근변환이 불안정하므로 건너뛴다.
    return abs(cv2.contourArea(quad.astype(np.float32))) > 1.0


def warp_with_grid(frame, source_grid, target_grid, output_width, output_height):
    # 전체 화면을 한 번에 변환하지 않고 셀 단위로 나눠 변환한다.
    # 이렇게 해야 외곽선이 휘어진 형태도 그리드 기반으로 따라갈 수 있다.
    output = np.zeros((output_height, output_width, 3), dtype=np.uint8)
    for row in range(GRID_ROWS):
        for col in range(GRID_COLS):
            src_quad = np.array(
                [source_grid[row, col], source_grid[row, col + 1], source_grid[row + 1, col + 1], source_grid[row + 1, col]],
                dtype=np.float32,
            )
            dst_quad = np.array(
                [target_grid[row, col], target_grid[row, col + 1], target_grid[row + 1, col + 1], target_grid[row + 1, col]],
                dtype=np.float32,
            )
            if not is_valid_quad(src_quad) or not is_valid_quad(dst_quad):
                continue
            matrix = cv2.getPerspectiveTransform(src_quad, dst_quad)
            warped_cell = cv2.warpPerspective(frame, matrix, (output_width, output_height))
            mask = np.zeros((output_height, output_width), dtype=np.uint8)
            cv2.fillPoly(mask, [np.round(dst_quad).astype(np.int32)], 255)
            output[mask > 0] = warped_cell[mask > 0]
    return output


def draw_grid_overlay(image, grid):
    # 설정 모드에서 편집용 그리드를 그린다.
    # 노란 점은 직접 편집 가능한 외곽 점, 초록 점은 자동 계산된 내부 점이다.
    pts = np.round(grid).astype(np.int32)
    for row in range(GRID_ROWS + 1):
        for col in range(GRID_COLS):
            cv2.line(image, tuple(pts[row, col]), tuple(pts[row, col + 1]), (0, 255, 0), 1)
    for col in range(GRID_COLS + 1):
        for row in range(GRID_ROWS):
            cv2.line(image, tuple(pts[row, col]), tuple(pts[row + 1, col]), (0, 255, 0), 1)
    for row in range(GRID_ROWS + 1):
        for col in range(GRID_COLS + 1):
            if is_edge_point(row, col):
                color = (0, 0, 255) if selected_point == (row, col) else (0, 255, 255)
                cv2.circle(image, tuple(pts[row, col]), 7, color, -1)
            else:
                cv2.circle(image, tuple(pts[row, col]), 3, (0, 120, 0), -1)


def place_output_window(window_name, monitor, fullscreen):
    # OpenCV 출력 창을 빔프로젝터/두 번째 모니터 위치로 이동한다.
    cv2.moveWindow(window_name, monitor["left"], monitor["top"])
    cv2.resizeWindow(window_name, monitor["width"], monitor["height"])
    value = cv2.WINDOW_FULLSCREEN if fullscreen else cv2.WINDOW_NORMAL
    cv2.setWindowProperty(window_name, cv2.WND_PROP_FULLSCREEN, value)


def print_monitor_info(monitors, source_monitor, output_monitor):
    print("Detected monitors:")
    for index, monitor in enumerate(monitors, start=1):
        role = []
        if monitor is source_monitor:
            role.append("source")
        if monitor is output_monitor:
            role.append("output")
        if monitor["primary"]:
            role.append("primary")
        print(
            f"  {index}. {monitor['device']} {monitor['width']}x{monitor['height']} "
            f"at ({monitor['left']}, {monitor['top']}) - {', '.join(role) if role else 'unused'}"
        )


def post_key_message(hwnd, message, vk):
    # 대상 창에 WM_KEYDOWN/WM_KEYUP 메시지를 보낸다.
    scan_code = win32api.MapVirtualKey(vk, 0)
    lparam = 1 | (scan_code << 16)
    if message == win32con.WM_KEYUP:
        lparam |= 0xC0000000
    win32api.PostMessage(hwnd, message, vk, lparam)


def key_to_vk_char_modifiers(key):
    # OpenCV 키 값을 Windows 가상 키 코드와 문자 입력으로 변환한다.
    if key in SPECIAL_KEY_TO_VK:
        return SPECIAL_KEY_TO_VK[key], None, []
    key8 = key & 0xFF
    if key8 in BASIC_KEY_TO_VK:
        return BASIC_KEY_TO_VK[key8], None, []
    if 32 <= key8 <= 126:
        char = chr(key8)
        vk_scan = win32api.VkKeyScan(char)
        if vk_scan == -1:
            return None, None, []
        vk = vk_scan & 0xFF
        shift_state = (vk_scan >> 8) & 0xFF
        modifiers = []
        if shift_state & 1:
            modifiers.append(win32con.VK_SHIFT)
        if shift_state & 2:
            modifiers.append(win32con.VK_CONTROL)
        if shift_state & 4:
            modifiers.append(win32con.VK_MENU)
        return vk, char, modifiers
    return None, None, []


def forward_key_to_source(key):
    # 인터랙션 모드에서 키보드 입력을 원본 화면의 대상 창으로 전달한다.
    hwnd = get_keyboard_target_hwnd()
    if hwnd is None:
        print("No keyboard target. Click the projected screen once first.")
        return
    vk, char, modifiers = key_to_vk_char_modifiers(key)
    if vk is None:
        return
    try:
        for modifier in modifiers:
            post_key_message(hwnd, win32con.WM_KEYDOWN, modifier)
        post_key_message(hwnd, win32con.WM_KEYDOWN, vk)
        if char is not None:
            win32api.PostMessage(hwnd, win32con.WM_CHAR, ord(char), 1)
        post_key_message(hwnd, win32con.WM_KEYUP, vk)
        for modifier in reversed(modifiers):
            post_key_message(hwnd, win32con.WM_KEYUP, modifier)
    except Exception as exc:
        print(f"Keyboard forwarding failed: {exc}")


def print_mode():
    if is_setup_mode:
        print("Mode: setup. Drag only yellow edge points. Inner green points are automatic.")
    else:
        print("Mode: interaction. Mouse/keyboard events are forwarded. Press F8 for setup.")


def handle_key(key, out_w, out_h, window_name, output_monitor):
    # 설정 모드에서는 프로그램 제어 키로 처리하고,
    # 인터랙션 모드에서는 대부분의 키를 원본 화면으로 전달한다.
    global dst_grid, is_setup_mode, is_fullscreen
    if key < 0:
        return False
    key8 = key & 0xFF
    if key8 == 27:
        return True
    if key in KEY_F8_CODES:
        is_setup_mode = not is_setup_mode
        print_mode()
        return False
    if is_setup_mode:
        if key8 in (ord("s"), ord("S")):
            is_setup_mode = False
            print_mode()
        elif key8 in (ord("w"), ord("W")):
            save_config()
        elif key8 in (ord("r"), ord("R")):
            dst_grid = create_default_grid(out_w, out_h)
            print("Grid reset to full output screen.")
        elif key8 in (ord("l"), ord("L")):
            legacy_grid = load_legacy_config(out_w, out_h)
            if legacy_grid is not None:
                dst_grid = legacy_grid
                print("Legacy rectangle loaded as editable edge grid.")
        elif key8 in (ord("f"), ord("F")):
            is_fullscreen = not is_fullscreen
            place_output_window(window_name, output_monitor, is_fullscreen)
    else:
        forward_key_to_source(key)
    return False


def main():
    # 프로그램 시작점: 모니터 선택, 설정 로딩, 출력 창 생성, 캡처/매핑 루프 실행.
    global src_grid, dst_grid, source_monitor_info
    try:
        source_monitor, output_monitor, monitors = choose_source_and_output_monitors()
    except Exception as exc:
        print(f"Monitor setup failed: {exc}")
        return

    source_monitor_info = source_monitor
    print_monitor_info(monitors, source_monitor, output_monitor)
    if source_monitor is output_monitor:
        print("Only one monitor was found. The output can be captured again by the source capture.")

    src_w = source_monitor["width"]
    src_h = source_monitor["height"]
    out_w = output_monitor["width"]
    out_h = output_monitor["height"]

    src_grid = create_default_grid(src_w, src_h)
    load_config(out_w, out_h)
    dst_grid = rebuild_auto_inner_points(dst_grid)

    window_name = "Projection Mapping"
    cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
    cv2.imshow(window_name, np.zeros((out_h, out_w, 3), dtype=np.uint8))
    cv2.waitKey(1)
    place_output_window(window_name, output_monitor, is_fullscreen)
    cv2.setMouseCallback(window_name, mouse_callback)

    print("Projection mapping started.")
    print("Source: primary monitor, Output: second monitor/projector.")
    print("Setup: drag yellow edge points only. Inner green points update automatically.")
    print("S: interaction, F8: setup/interaction, W: save, R: reset, L: legacy, F: fullscreen, ESC: quit")
    print_mode()

    frame_count = 0
    while True:
        # 매 프레임마다 원본 모니터를 캡처하고, 현재 그리드 형태로 변형해 출력한다.
        try:
            frame = capture_screen_region(source_monitor["left"], source_monitor["top"], src_w, src_h)
        except Exception as exc:
            print(f"Capture failed: {exc}")
            break

        mapped_frame = warp_with_grid(frame, src_grid, dst_grid, out_w, out_h)
        if frame_count == 0:
            nonzero_ratio = float(np.count_nonzero(mapped_frame)) / float(mapped_frame.size)
            print(f"First capture brightness: {frame.mean():.1f}")
            print(f"First mapped brightness: {mapped_frame.mean():.1f}, nonzero ratio: {nonzero_ratio:.3f}")
        if is_setup_mode:
            draw_grid_overlay(mapped_frame, dst_grid)
        cv2.imshow(window_name, mapped_frame)

        key = cv2.waitKeyEx(1)
        if handle_key(key, out_w, out_h, window_name, output_monitor):
            break
        frame_count += 1

    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
