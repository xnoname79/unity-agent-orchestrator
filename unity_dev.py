"""
Unity Game Dev MCP Server

MCP server providing tools for Unity 3D walking simulator development:
  - Story & Narrative: dialogues, notes, collectibles, events
  - Scene Planning: manage scenes with mood, story beats
  - Asset Tracking: models, textures, sounds by type/status
  - Game Design Document: sectioned GDD
  - Script Templates: ready-to-use C# scripts

Architecture:
  - Standalone HTTP server (streamable-http)
  - Each game project gets its own SQLite DB at ~/.unity_dev_db/<project>.db

Start:  python3 unity_dev.py
"""

import json
import os
import sqlite3
import asyncio
from datetime import datetime
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.requests import Request
from starlette.responses import JSONResponse
from mcp.server.fastmcp import FastMCP

DB_DIR = Path.home() / ".unity_dev_db"
HOST = os.environ.get("UNITY_DEV_HOST", "0.0.0.0")
PORT = int(os.environ.get("UNITY_DEV_PORT", "8990"))

_db_lock = asyncio.Lock()


def _db_path(project: str) -> str:
    DB_DIR.mkdir(parents=True, exist_ok=True)
    return str(DB_DIR / f"{project}.db")


def _get_conn(project: str):
    conn = sqlite3.connect(_db_path(project))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _init_db(project: str):
    conn = _get_conn(project)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS story_elements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            type TEXT NOT NULL,
            title TEXT NOT NULL,
            content TEXT NOT NULL DEFAULT '',
            scene TEXT NOT NULL DEFAULT '',
            trigger_type TEXT NOT NULL DEFAULT 'interact',
            order_index INTEGER NOT NULL DEFAULT 0,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'draft',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS scenes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            mood TEXT NOT NULL DEFAULT '',
            story_beats TEXT NOT NULL DEFAULT '',
            order_index INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'planned',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            type TEXT NOT NULL,
            description TEXT NOT NULL DEFAULT '',
            scene TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'needed',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS gdd (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            section TEXT NOT NULL UNIQUE,
            content TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        );
    """)
    conn.commit()
    conn.close()


def _ensure_db(project: str):
    if not os.path.exists(_db_path(project)):
        _init_db(project)


def _row_to_dict(row):
    return dict(row)


def _backup_db(project: str):
    db_path = _db_path(project)
    if not os.path.exists(db_path):
        return ""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    bak_path = f"{db_path}.{timestamp}.bak"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(bak_path)
    src.backup(dst)
    src.close()
    dst.close()
    return bak_path


@asynccontextmanager
async def lifespan(server):
    yield {}


mcp = FastMCP(
    "Unity-Game-Dev",
    lifespan=lifespan,
    host=HOST,
    port=PORT,
)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request):
    projects = sorted(f.stem for f in DB_DIR.glob("*.db")) if DB_DIR.exists() else []
    return JSONResponse({"status": "ok", "server": "Unity-Game-Dev", "projects": projects})


# ─── Story & Narrative ────────────────────────────────────────────────────────


@mcp.tool()
async def add_story_element(
    project: str,
    type: str,
    title: str,
    content: str = "",
    scene: str = "",
    trigger_type: str = "interact",
    order_index: int = 0,
    metadata_json: str = "{}",
):
    """Tạo story element mới cho game (dialogue, note, event, collectible, voiceover, environment).

    Args:
        project: Tên project game
        type: Loại element: dialogue, note, collectible, event, voiceover, environment
        title: Tên ngắn gọn
        content: Nội dung text (dialogue text, note content, event description, ...)
        scene: Scene chứa element này
        trigger_type: Cách kích hoạt: interact, proximity, auto, pickup, cutscene
        order_index: Thứ tự hiển thị/trigger (0 = mặc định)
        metadata_json: JSON data bổ sung (vd: {"speaker": "NPC1", "emotion": "sad", "choices": [...]})
    """
    async with _db_lock:
        _ensure_db(project)
        now = datetime.now().isoformat()
        conn = _get_conn(project)
        conn.execute(
            "INSERT INTO story_elements (type, title, content, scene, trigger_type, order_index, metadata_json, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)",
            (type, title, content, scene, trigger_type, order_index, metadata_json, now, now),
        )
        conn.commit()
        conn.close()
    return f"[{project}] Đã tạo {type}: '{title}'" + (f" (scene: {scene})" if scene else "")


@mcp.tool()
async def list_story_elements(project: str, type: str = "", scene: str = "", status: str = ""):
    """Liệt kê story elements.

    Args:
        project: Tên project game
        type: Lọc theo loại (dialogue, note, collectible, ...). Để trống = tất cả.
        scene: Lọc theo scene. Để trống = tất cả.
        status: Lọc theo status (draft, final). Để trống = tất cả.
    """
    _ensure_db(project)
    conn = _get_conn(project)
    query = "SELECT * FROM story_elements WHERE 1=1"
    params = []
    if type:
        query += " AND type = ?"
        params.append(type)
    if scene:
        query += " AND scene = ?"
        params.append(scene)
    if status:
        query += " AND status = ?"
        params.append(status)
    query += " ORDER BY scene, order_index, id"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    if not rows:
        return f"[{project}] Không có story elements nào."
    return json.dumps([_row_to_dict(r) for r in rows], ensure_ascii=False)


@mcp.tool()
async def update_story_element(
    project: str,
    id: int,
    title: str = "",
    content: str = "",
    scene: str = "",
    trigger_type: str = "",
    order_index: int = -1,
    metadata_json: str = "",
    status: str = "",
):
    """Cập nhật story element theo id.

    Args:
        project: Tên project game
        id: ID của story element
        title: Tiêu đề mới (để trống = không đổi)
        content: Nội dung mới (để trống = không đổi)
        scene: Scene mới (để trống = không đổi)
        trigger_type: Trigger mới (để trống = không đổi)
        order_index: Thứ tự mới (-1 = không đổi)
        metadata_json: Metadata mới (để trống = không đổi)
        status: Status mới: draft, final (để trống = không đổi)
    """
    async with _db_lock:
        _ensure_db(project)
        conn = _get_conn(project)
        row = conn.execute("SELECT * FROM story_elements WHERE id = ?", (id,)).fetchone()
        if not row:
            conn.close()
            return f"[{project}] Lỗi: story element id {id} không tồn tại."
        updates, params = [], []
        if title:
            updates.append("title = ?"); params.append(title)
        if content:
            updates.append("content = ?"); params.append(content)
        if scene:
            updates.append("scene = ?"); params.append(scene)
        if trigger_type:
            updates.append("trigger_type = ?"); params.append(trigger_type)
        if order_index >= 0:
            updates.append("order_index = ?"); params.append(order_index)
        if metadata_json:
            updates.append("metadata_json = ?"); params.append(metadata_json)
        if status:
            updates.append("status = ?"); params.append(status)
        if updates:
            updates.append("updated_at = ?"); params.append(datetime.now().isoformat())
            params.append(id)
            conn.execute(f"UPDATE story_elements SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
        updated = conn.execute("SELECT * FROM story_elements WHERE id = ?", (id,)).fetchone()
        conn.close()
    return f"[{project}] Đã cập nhật {updated['type']}: '{updated['title']}'"


@mcp.tool()
async def delete_story_element(project: str, id: int):
    """Xóa story element theo id.

    Args:
        project: Tên project game
        id: ID của story element cần xóa
    """
    async with _db_lock:
        _ensure_db(project)
        conn = _get_conn(project)
        row = conn.execute("SELECT * FROM story_elements WHERE id = ?", (id,)).fetchone()
        if not row:
            conn.close()
            return f"[{project}] Lỗi: story element id {id} không tồn tại."
        conn.execute("DELETE FROM story_elements WHERE id = ?", (id,))
        conn.commit()
        conn.close()
    return f"[{project}] Đã xóa {row['type']}: '{row['title']}'"


@mcp.tool()
async def export_narrative_json(project: str, scene: str = ""):
    """Export story elements thành JSON để Unity đọc runtime.

    Args:
        project: Tên project game
        scene: Export cho scene cụ thể. Để trống = export tất cả.
    """
    _ensure_db(project)
    conn = _get_conn(project)
    if scene:
        rows = conn.execute("SELECT * FROM story_elements WHERE scene = ? ORDER BY order_index, id", (scene,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM story_elements ORDER BY scene, order_index, id").fetchall()
    conn.close()
    if not rows:
        return f"[{project}] Không có story elements để export."
    elements = []
    for r in rows:
        el = _row_to_dict(r)
        try:
            el["metadata"] = json.loads(el.pop("metadata_json"))
        except (json.JSONDecodeError, KeyError):
            el["metadata"] = {}
        elements.append(el)
    return json.dumps({"story_elements": elements}, indent=2, ensure_ascii=False)


# ─── Scene Planning ──────────────────────────────────────────────────────────


@mcp.tool()
async def add_scene(
    project: str,
    name: str,
    description: str = "",
    mood: str = "",
    story_beats: str = "",
    order_index: int = 0,
):
    """Tạo scene/level mới cho game.

    Args:
        project: Tên project game
        name: Tên scene (vd: "ForestEntrance", "AbandonedHouse")
        description: Mô tả scene — environment, layout
        mood: Không khí/atmosphere (vd: "dark, mysterious, foggy")
        story_beats: Các điểm nhấn câu chuyện trong scene
        order_index: Thứ tự scene trong game (0 = mặc định)
    """
    async with _db_lock:
        _ensure_db(project)
        now = datetime.now().isoformat()
        conn = _get_conn(project)
        conn.execute(
            "INSERT INTO scenes (name, description, mood, story_beats, order_index, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'planned', ?, ?)",
            (name, description, mood, story_beats, order_index, now, now),
        )
        conn.commit()
        conn.close()
    return f"[{project}] Đã tạo scene: '{name}'"


@mcp.tool()
async def list_scenes(project: str, status: str = ""):
    """Liệt kê tất cả scenes.

    Args:
        project: Tên project game
        status: Lọc theo status (planned, in_progress, done). Để trống = tất cả.
    """
    _ensure_db(project)
    conn = _get_conn(project)
    if status:
        rows = conn.execute("SELECT * FROM scenes WHERE status = ? ORDER BY order_index, id", (status,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM scenes ORDER BY order_index, id").fetchall()
    conn.close()
    if not rows:
        return f"[{project}] Chưa có scene nào."
    return json.dumps([_row_to_dict(r) for r in rows], ensure_ascii=False)


@mcp.tool()
async def update_scene(
    project: str,
    id: int,
    name: str = "",
    description: str = "",
    mood: str = "",
    story_beats: str = "",
    order_index: int = -1,
    status: str = "",
):
    """Cập nhật scene theo id.

    Args:
        project: Tên project game
        id: ID của scene
        name: Tên mới (để trống = không đổi)
        description: Mô tả mới (để trống = không đổi)
        mood: Mood mới (để trống = không đổi)
        story_beats: Story beats mới (để trống = không đổi)
        order_index: Thứ tự mới (-1 = không đổi)
        status: Status mới: planned, in_progress, done (để trống = không đổi)
    """
    async with _db_lock:
        _ensure_db(project)
        conn = _get_conn(project)
        row = conn.execute("SELECT * FROM scenes WHERE id = ?", (id,)).fetchone()
        if not row:
            conn.close()
            return f"[{project}] Lỗi: scene id {id} không tồn tại."
        updates, params = [], []
        if name:
            updates.append("name = ?"); params.append(name)
        if description:
            updates.append("description = ?"); params.append(description)
        if mood:
            updates.append("mood = ?"); params.append(mood)
        if story_beats:
            updates.append("story_beats = ?"); params.append(story_beats)
        if order_index >= 0:
            updates.append("order_index = ?"); params.append(order_index)
        if status:
            updates.append("status = ?"); params.append(status)
        if updates:
            updates.append("updated_at = ?"); params.append(datetime.now().isoformat())
            params.append(id)
            conn.execute(f"UPDATE scenes SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
        updated = conn.execute("SELECT * FROM scenes WHERE id = ?", (id,)).fetchone()
        conn.close()
    return f"[{project}] Đã cập nhật scene: '{updated['name']}'"


# ─── Asset Tracking ──────────────────────────────────────────────────────────


@mcp.tool()
async def add_asset(
    project: str,
    name: str,
    type: str,
    description: str = "",
    scene: str = "",
    source: str = "",
):
    """Đăng ký asset cần thiết cho game.

    Args:
        project: Tên project game
        name: Tên asset (vd: "old_tree", "footstep_wood", "ambient_wind")
        type: Loại: model, texture, sound, music, animation, shader, prefab, font
        description: Mô tả chi tiết (hình dáng, âm thanh, kích thước, ...)
        scene: Scene sử dụng asset này
        source: Nguồn asset (vd: "Unity Asset Store", "Mixamo", "tự tạo", "Poly Haven")
    """
    async with _db_lock:
        _ensure_db(project)
        now = datetime.now().isoformat()
        conn = _get_conn(project)
        conn.execute(
            "INSERT INTO assets (name, type, description, scene, source, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'needed', ?, ?)",
            (name, type, description, scene, source, now, now),
        )
        conn.commit()
        conn.close()
    return f"[{project}] Đã thêm asset: '{name}' ({type})"


@mcp.tool()
async def list_assets(project: str, type: str = "", status: str = "", scene: str = ""):
    """Liệt kê assets.

    Args:
        project: Tên project game
        type: Lọc theo loại (model, texture, sound, ...). Để trống = tất cả.
        status: Lọc theo status (needed, found, done). Để trống = tất cả.
        scene: Lọc theo scene. Để trống = tất cả.
    """
    _ensure_db(project)
    conn = _get_conn(project)
    query = "SELECT * FROM assets WHERE 1=1"
    params = []
    if type:
        query += " AND type = ?"
        params.append(type)
    if status:
        query += " AND status = ?"
        params.append(status)
    if scene:
        query += " AND scene = ?"
        params.append(scene)
    query += " ORDER BY type, name"
    rows = conn.execute(query, params).fetchall()
    conn.close()
    if not rows:
        return f"[{project}] Không có assets nào."
    return json.dumps([_row_to_dict(r) for r in rows], ensure_ascii=False)


@mcp.tool()
async def update_asset(
    project: str,
    id: int,
    name: str = "",
    description: str = "",
    scene: str = "",
    source: str = "",
    status: str = "",
):
    """Cập nhật asset theo id.

    Args:
        project: Tên project game
        id: ID của asset
        name: Tên mới (để trống = không đổi)
        description: Mô tả mới (để trống = không đổi)
        scene: Scene mới (để trống = không đổi)
        source: Nguồn mới (để trống = không đổi)
        status: Status mới: needed, found, done (để trống = không đổi)
    """
    async with _db_lock:
        _ensure_db(project)
        conn = _get_conn(project)
        row = conn.execute("SELECT * FROM assets WHERE id = ?", (id,)).fetchone()
        if not row:
            conn.close()
            return f"[{project}] Lỗi: asset id {id} không tồn tại."
        updates, params = [], []
        if name:
            updates.append("name = ?"); params.append(name)
        if description:
            updates.append("description = ?"); params.append(description)
        if scene:
            updates.append("scene = ?"); params.append(scene)
        if source:
            updates.append("source = ?"); params.append(source)
        if status:
            updates.append("status = ?"); params.append(status)
        if updates:
            updates.append("updated_at = ?"); params.append(datetime.now().isoformat())
            params.append(id)
            conn.execute(f"UPDATE assets SET {', '.join(updates)} WHERE id = ?", params)
        conn.commit()
        updated = conn.execute("SELECT * FROM assets WHERE id = ?", (id,)).fetchone()
        conn.close()
    return f"[{project}] Đã cập nhật asset: '{updated['name']}'"


# ─── Game Design Document ────────────────────────────────────────────────────


@mcp.tool()
async def update_gdd(project: str, section: str, content: str):
    """Cập nhật một phần của Game Design Document.

    Args:
        project: Tên project game
        section: Phần GDD: overview, mechanics, story, art_style, audio, levels, controls, characters
        content: Nội dung của phần này
    """
    async with _db_lock:
        _ensure_db(project)
        now = datetime.now().isoformat()
        conn = _get_conn(project)
        conn.execute(
            "INSERT INTO gdd (section, content, updated_at) VALUES (?, ?, ?) ON CONFLICT(section) DO UPDATE SET content = ?, updated_at = ?",
            (section, content, now, content, now),
        )
        conn.commit()
        conn.close()
    return f"[{project}] Đã cập nhật GDD section: '{section}'"


@mcp.tool()
async def get_gdd(project: str, section: str = ""):
    """Xem Game Design Document.

    Args:
        project: Tên project game
        section: Xem section cụ thể. Để trống = xem toàn bộ GDD.
    """
    _ensure_db(project)
    conn = _get_conn(project)
    if section:
        rows = conn.execute("SELECT * FROM gdd WHERE section = ?", (section,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM gdd ORDER BY section").fetchall()
    conn.close()
    if not rows:
        return f"[{project}] GDD chưa có nội dung." + (f" (section: {section})" if section else "")
    return json.dumps([_row_to_dict(r) for r in rows], ensure_ascii=False)


# ─── Script Templates ────────────────────────────────────────────────────────

SCRIPT_TEMPLATES = {
    "FirstPersonController": '''using UnityEngine;

public class FirstPersonController : MonoBehaviour
{
    [Header("Movement")]
    public float walkSpeed = 3f;
    public float runSpeed = 6f;
    public float gravity = -9.81f;

    [Header("Look")]
    public float mouseSensitivity = 2f;
    public Transform cameraHolder;

    private CharacterController controller;
    private Vector3 velocity;
    private float xRotation;

    void Start()
    {
        controller = GetComponent<CharacterController>();
        Cursor.lockState = CursorLockMode.Locked;
    }

    void Update()
    {
        HandleLook();
        HandleMovement();
    }

    void HandleLook()
    {
        float mouseX = Input.GetAxis("Mouse X") * mouseSensitivity;
        float mouseY = Input.GetAxis("Mouse Y") * mouseSensitivity;
        xRotation -= mouseY;
        xRotation = Mathf.Clamp(xRotation, -80f, 80f);
        cameraHolder.localRotation = Quaternion.Euler(xRotation, 0f, 0f);
        transform.Rotate(Vector3.up * mouseX);
    }

    void HandleMovement()
    {
        float speed = Input.GetKey(KeyCode.LeftShift) ? runSpeed : walkSpeed;
        float x = Input.GetAxis("Horizontal");
        float z = Input.GetAxis("Vertical");
        Vector3 move = transform.right * x + transform.forward * z;
        controller.Move(move * speed * Time.deltaTime);

        if (controller.isGrounded && velocity.y < 0)
            velocity.y = -2f;
        velocity.y += gravity * Time.deltaTime;
        controller.Move(velocity * Time.deltaTime);
    }
}''',
    "Interactable": '''using UnityEngine;
using UnityEngine.Events;

public class Interactable : MonoBehaviour
{
    [Header("Settings")]
    public float interactRange = 2f;
    public string promptText = "Press E to interact";
    public bool oneTimeOnly = false;

    [Header("Events")]
    public UnityEvent onInteract;

    private bool hasInteracted;

    public bool CanInteract(Transform player)
    {
        if (oneTimeOnly && hasInteracted) return false;
        return Vector3.Distance(transform.position, player.position) <= interactRange;
    }

    public void Interact()
    {
        if (oneTimeOnly && hasInteracted) return;
        hasInteracted = true;
        onInteract?.Invoke();
    }
}''',
    "InteractionSystem": '''using UnityEngine;
using TMPro;

public class InteractionSystem : MonoBehaviour
{
    public float rayDistance = 3f;
    public LayerMask interactableLayer;
    public TMP_Text promptUI;

    private Camera cam;
    private Interactable currentTarget;

    void Start()
    {
        cam = Camera.main;
        if (promptUI) promptUI.gameObject.SetActive(false);
    }

    void Update()
    {
        Ray ray = cam.ScreenPointToRay(new Vector3(Screen.width / 2, Screen.height / 2));
        if (Physics.Raycast(ray, out RaycastHit hit, rayDistance, interactableLayer))
        {
            var interactable = hit.collider.GetComponent<Interactable>();
            if (interactable != null && interactable.CanInteract(transform))
            {
                currentTarget = interactable;
                if (promptUI)
                {
                    promptUI.text = interactable.promptText;
                    promptUI.gameObject.SetActive(true);
                }
                if (Input.GetKeyDown(KeyCode.E))
                    interactable.Interact();
                return;
            }
        }
        currentTarget = null;
        if (promptUI) promptUI.gameObject.SetActive(false);
    }
}''',
    "DialogueUI": '''using UnityEngine;
using TMPro;
using System.Collections;

public class DialogueUI : MonoBehaviour
{
    public static DialogueUI Instance;

    [Header("UI References")]
    public GameObject dialoguePanel;
    public TMP_Text speakerText;
    public TMP_Text contentText;

    [Header("Settings")]
    public float typingSpeed = 0.03f;

    private bool isTyping;
    private string fullText;

    void Awake() => Instance = this;
    void Start() => dialoguePanel.SetActive(false);

    public void ShowDialogue(string speaker, string content)
    {
        dialoguePanel.SetActive(true);
        speakerText.text = speaker;
        fullText = content;
        StartCoroutine(TypeText());
    }

    IEnumerator TypeText()
    {
        isTyping = true;
        contentText.text = "";
        foreach (char c in fullText)
        {
            contentText.text += c;
            yield return new WaitForSeconds(typingSpeed);
        }
        isTyping = false;
    }

    public void CloseDialogue()
    {
        StopAllCoroutines();
        dialoguePanel.SetActive(false);
    }

    void Update()
    {
        if (dialoguePanel.activeSelf && Input.GetKeyDown(KeyCode.Space))
        {
            if (isTyping)
            {
                StopAllCoroutines();
                contentText.text = fullText;
                isTyping = false;
            }
            else
            {
                CloseDialogue();
            }
        }
    }
}''',
    "NotePickup": '''using UnityEngine;

public class NotePickup : MonoBehaviour
{
    [Header("Content")]
    [TextArea(3, 10)]
    public string noteTitle = "Note";
    [TextArea(5, 20)]
    public string noteContent = "Note content here...";
    public string speaker = "";

    public void ShowNote()
    {
        if (DialogueUI.Instance != null)
            DialogueUI.Instance.ShowDialogue(
                string.IsNullOrEmpty(speaker) ? noteTitle : speaker,
                noteContent
            );
    }
}''',
    "DoorController": '''using UnityEngine;

public class DoorController : MonoBehaviour
{
    public float openAngle = 90f;
    public float openSpeed = 2f;
    public bool requiresKey;
    public string keyId = "";

    private bool isOpen;
    private Quaternion closedRotation;
    private Quaternion openRotation;

    void Start()
    {
        closedRotation = transform.rotation;
        openRotation = Quaternion.Euler(transform.eulerAngles + Vector3.up * openAngle);
    }

    void Update()
    {
        Quaternion target = isOpen ? openRotation : closedRotation;
        transform.rotation = Quaternion.Slerp(transform.rotation, target, openSpeed * Time.deltaTime);
    }

    public void ToggleDoor()
    {
        isOpen = !isOpen;
    }
}''',
    "AudioZone": '''using UnityEngine;

[RequireComponent(typeof(AudioSource))]
public class AudioZone : MonoBehaviour
{
    [Header("Settings")]
    public float fadeInDuration = 1f;
    public float fadeOutDuration = 1f;
    public float maxVolume = 1f;

    private AudioSource audioSource;
    private bool playerInside;
    private float targetVolume;

    void Start()
    {
        audioSource = GetComponent<AudioSource>();
        audioSource.volume = 0f;
        audioSource.loop = true;
        audioSource.playOnAwake = false;
    }

    void Update()
    {
        targetVolume = playerInside ? maxVolume : 0f;
        float fadeSpeed = playerInside ? 1f / fadeInDuration : 1f / fadeOutDuration;
        audioSource.volume = Mathf.MoveTowards(audioSource.volume, targetVolume, fadeSpeed * Time.deltaTime);

        if (audioSource.volume > 0 && !audioSource.isPlaying)
            audioSource.Play();
        else if (audioSource.volume <= 0 && audioSource.isPlaying)
            audioSource.Stop();
    }

    void OnTriggerEnter(Collider other)
    {
        if (other.CompareTag("Player")) playerInside = true;
    }

    void OnTriggerExit(Collider other)
    {
        if (other.CompareTag("Player")) playerInside = false;
    }
}''',
    "CutsceneTrigger": '''using UnityEngine;
using UnityEngine.Events;

public class CutsceneTrigger : MonoBehaviour
{
    public bool triggerOnce = true;
    public UnityEvent onCutsceneStart;
    public UnityEvent onCutsceneEnd;
    public float duration = 5f;

    private bool triggered;

    void OnTriggerEnter(Collider other)
    {
        if (!other.CompareTag("Player")) return;
        if (triggerOnce && triggered) return;
        triggered = true;
        StartCoroutine(PlayCutscene());
    }

    System.Collections.IEnumerator PlayCutscene()
    {
        onCutsceneStart?.Invoke();
        yield return new WaitForSeconds(duration);
        onCutsceneEnd?.Invoke();
    }
}''',
}


@mcp.tool()
async def generate_script(project: str, template: str):
    """Generate C# script từ template cho Unity.

    Args:
        project: Tên project game
        template: Tên template. Dùng list_templates() để xem danh sách.
    """
    if template not in SCRIPT_TEMPLATES:
        available = ", ".join(sorted(SCRIPT_TEMPLATES.keys()))
        return f"[{project}] Template '{template}' không tồn tại. Có sẵn: {available}"
    return SCRIPT_TEMPLATES[template]


@mcp.tool()
async def list_templates():
    """Liệt kê tất cả C# script templates có sẵn cho Unity walking simulator."""
    descriptions = {
        "FirstPersonController": "Di chuyển + camera first-person (WASD + mouse, shift chạy)",
        "Interactable": "Component cho objects có thể tương tác (press E)",
        "InteractionSystem": "Raycast system để detect và interact với objects",
        "DialogueUI": "UI hiển thị dialogue với typing effect",
        "NotePickup": "Ghi chú có thể nhặt và đọc",
        "DoorController": "Cửa mở/đóng với animation",
        "AudioZone": "Vùng trigger âm thanh với fade in/out",
        "CutsceneTrigger": "Trigger cutscene khi player đi vào vùng",
    }
    result = [{"template": name, "description": desc} for name, desc in descriptions.items()]
    return json.dumps(result, ensure_ascii=False)


@mcp.tool()
async def list_game_projects():
    """Liệt kê tất cả game projects hiện có."""
    if not DB_DIR.exists():
        return "Chưa có game project nào."
    projects = sorted(f.stem for f in DB_DIR.glob("*.db"))
    if not projects:
        return "Chưa có game project nào."
    return json.dumps(projects, ensure_ascii=False)


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
