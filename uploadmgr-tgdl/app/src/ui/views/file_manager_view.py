import os
import subprocess
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, 
    QLineEdit, QComboBox, QTableWidget, QTableWidgetItem, QHeaderView,
    QAbstractItemView, QMenu, QMessageBox, QApplication, QFrame, QCheckBox
)
from PySide6.QtCore import Qt, Signal, QUrl
from PySide6.QtGui import QDesktopServices, QIcon, QColor

from database import (
    get_all_completed_media, delete_media_cache_item, 
    delete_multiple_media_cache_items, get_cached_channels_summary,
    unmark_media_completed, update_media_downloaded_path
)
from ui.views.settings_view import load_config


def format_bytes(size_bytes):
    if not size_bytes or size_bytes <= 0:
        return "0 B"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}" if unit != "B" else f"{int(size_bytes)} B"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"


def resolve_item_disk_path(item):
    """
    Finds the actual disk file path for a media item.
    If downloaded_path is empty, searches configured downloads folder.
    """
    fpath = item.get("downloaded_path")
    if fpath and os.path.exists(fpath) and os.path.getsize(fpath) > 0:
        return fpath, True

    cfg = load_config()
    base_down = cfg.get("download_path", "downloads")
    fname = item.get("title") or ""
    c_title = item.get("resolved_channel_title") or item.get("channel_title") or ""

    if fname:
        # 1. Direct inside base_down
        cand1 = os.path.join(base_down, fname)
        if os.path.exists(cand1) and os.path.getsize(cand1) > 0:
            update_media_downloaded_path(item.get("channel_id"), item.get("msg_id"), cand1)
            return cand1, True

        # 2. Check channel subfolders
        if c_title:
            safe_c = "".join([c if c.isalnum() or c in (' ', '-', '_') else '_' for c in c_title])
            for cat in ["", "videos", "images", "pdfs", "zips", "audio", "all_media"]:
                cand2 = os.path.join(base_down, safe_c, cat, fname)
                if os.path.exists(cand2) and os.path.getsize(cand2) > 0:
                    update_media_downloaded_path(item.get("channel_id"), item.get("msg_id"), cand2)
                    return cand2, True

    return fpath, bool(fpath and os.path.exists(fpath) and os.path.getsize(fpath) > 0)


class FileManagerView(QWidget):
    reDownloadRequested = Signal(str, int) # (channel_id, msg_id)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.all_items = []
        self.channel_summaries = []
        self.setup_ui()
        self.refresh_list()

    def setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(32, 20, 32, 24)
        main_layout.setSpacing(16)

        # ── 1. Top Header & Stats ─────────────────────────────────────────────
        top_row = QHBoxLayout()
        self.lbl_header = QLabel("📁 Downloaded Files Manager")
        self.lbl_header.setObjectName("MainHeader")

        self.lbl_stats = QLabel("0 files (0 B)")
        self.lbl_stats.setObjectName("MutedText")
        self.lbl_stats.setStyleSheet("font-size: 12px; font-weight: 600;")

        top_row.addWidget(self.lbl_header)
        top_row.addSpacing(12)
        top_row.addWidget(self.lbl_stats)
        top_row.addStretch()

        self.btn_open_root = QPushButton("📂 Open Downloads Folder")
        self.btn_open_root.setObjectName("SecondaryButton")
        self.btn_open_root.setCursor(Qt.PointingHandCursor)
        self.btn_open_root.clicked.connect(self.open_downloads_folder)

        self.btn_refresh = QPushButton("🔄 Refresh")
        self.btn_refresh.setObjectName("SecondaryButton")
        self.btn_refresh.setCursor(Qt.PointingHandCursor)
        self.btn_refresh.clicked.connect(self.refresh_list)

        top_row.addWidget(self.btn_open_root)
        top_row.addWidget(self.btn_refresh)
        main_layout.addLayout(top_row)

        # ── 2. Filters Bar ───────────────────────────────────────────────────
        filter_frame = QFrame()
        filter_frame.setObjectName("WhiteCard")
        filter_frame.setProperty("compact", True)
        f_layout = QHBoxLayout(filter_frame)
        f_layout.setContentsMargins(12, 10, 12, 10)
        f_layout.setSpacing(12)

        # Search Bar
        self.input_search = QLineEdit()
        self.input_search.setPlaceholderText("🔍 Search by filename, ID, or channel...")
        self.input_search.textChanged.connect(self.apply_filter)
        f_layout.addWidget(self.input_search, stretch=3)

        # Channel Selector Dropdown
        self.combo_channel = QComboBox()
        self.combo_channel.addItem("📺 All Channels", "all")
        self.combo_channel.currentIndexChanged.connect(self.apply_filter)
        f_layout.addWidget(self.combo_channel, stretch=2)

        # Category Filter
        self.combo_category = QComboBox()
        self.combo_category.addItems([
            "All Categories", "Images", "Videos", "Documents / PDFs", "ZIPs", "Audio"
        ])
        self.combo_category.currentIndexChanged.connect(self.apply_filter)
        f_layout.addWidget(self.combo_category, stretch=2)

        # Status Filter
        self.combo_status = QComboBox()
        self.combo_status.addItems([
            "All Status", "🟢 Present on Disk", "🟡 Missing / Deleted"
        ])
        self.combo_status.currentIndexChanged.connect(self.apply_filter)
        f_layout.addWidget(self.combo_status, stretch=2)

        main_layout.addWidget(filter_frame)

        # ── 3. Table View ────────────────────────────────────────────────────
        self.table = QTableWidget()
        self.table.setObjectName("FileTable")
        self.table.setColumnCount(8)
        self.table.setHorizontalHeaderLabels([
            " ", "Type", "File Name", "Channel", "Size", "Date", "Status", "Actions"
        ])
        
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Fixed) # Checkbox
        self.table.setColumnWidth(0, 36)
        header.setSectionResizeMode(1, QHeaderView.Fixed) # Type
        self.table.setColumnWidth(1, 46)
        header.setSectionResizeMode(2, QHeaderView.Stretch) # File Name
        header.setSectionResizeMode(3, QHeaderView.Fixed) # Channel
        self.table.setColumnWidth(3, 150)
        header.setSectionResizeMode(4, QHeaderView.Fixed) # Size
        self.table.setColumnWidth(4, 85)
        header.setSectionResizeMode(5, QHeaderView.Fixed) # Date
        self.table.setColumnWidth(5, 95)
        header.setSectionResizeMode(6, QHeaderView.Fixed) # Status
        self.table.setColumnWidth(6, 95)
        header.setSectionResizeMode(7, QHeaderView.Fixed) # Actions
        self.table.setColumnWidth(7, 210)

        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(40)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.show_context_menu)
        self.table.doubleClicked.connect(self.on_row_double_click)

        main_layout.addWidget(self.table, stretch=1)

        # ── 4. Bottom Bulk Action Bar ────────────────────────────────────────
        bottom_bar = QHBoxLayout()
        
        self.chk_select_all = QCheckBox("Select All")
        self.chk_select_all.stateChanged.connect(self.toggle_select_all)
        bottom_bar.addWidget(self.chk_select_all)

        self.lbl_selected_count = QLabel("0 selected")
        self.lbl_selected_count.setObjectName("MutedText")
        bottom_bar.addWidget(self.lbl_selected_count)

        bottom_bar.addStretch()

        self.btn_bulk_remove_db = QPushButton("🗑 Remove from List")
        self.btn_bulk_remove_db.setObjectName("SecondaryButton")
        self.btn_bulk_remove_db.setToolTip("Remove records from download history without deleting files on disk")
        self.btn_bulk_remove_db.clicked.connect(self.bulk_remove_from_list)
        bottom_bar.addWidget(self.btn_bulk_remove_db)

        self.btn_bulk_delete_disk = QPushButton("❌ Delete from Disk & List")
        self.btn_bulk_delete_disk.setObjectName("DangerButton")
        self.btn_bulk_delete_disk.setStyleSheet("background-color: #EF4444; color: white; font-weight: 600; padding: 6px 14px; border-radius: 6px;")
        self.btn_bulk_delete_disk.setToolTip("Permanently delete selected files from disk and history")
        self.btn_bulk_delete_disk.clicked.connect(self.bulk_delete_from_disk)
        bottom_bar.addWidget(self.btn_bulk_delete_disk)

        main_layout.addLayout(bottom_bar)

    def refresh_list(self):
        """Loads all completed media from SQLite, updates channel filters, and re-renders table."""
        self.all_items = get_all_completed_media()
        self.update_channel_dropdown()
        self.apply_filter()

    def update_channel_dropdown(self):
        """Populates the channel filter dropdown with available channels."""
        current_data = self.combo_channel.currentData()
        self.combo_channel.blockSignals(True)
        self.combo_channel.clear()
        self.combo_channel.addItem("📺 All Channels", "all")

        summaries = get_cached_channels_summary()
        for s in summaries:
            c_id = s.get("channel_id")
            c_title = s.get("channel_title") or c_id
            c_count = s.get("count", 0)
            label = f"📢 {c_title} ({c_count})"
            self.combo_channel.addItem(label, str(c_id))

        # Restore previous selection if possible
        if current_data:
            idx = self.combo_channel.findData(current_data)
            if idx >= 0:
                self.combo_channel.setCurrentIndex(idx)
        self.combo_channel.blockSignals(False)

    def apply_filter(self):
        search_text = self.input_search.text().strip().lower()
        selected_chan = self.combo_channel.currentData()
        cat_idx = self.combo_category.currentIndex()
        status_idx = self.combo_status.currentIndex()

        cat_map = {
            1: ["photo", "image", "images"],
            2: ["video", "videos"],
            3: ["pdf", "pdfs", "document", "documents"],
            4: ["zip", "zips", "archive"],
            5: ["audio", "music", "voice"]
        }

        filtered = []
        total_disk_size = 0

        for item in self.all_items:
            fpath, on_disk = resolve_item_disk_path(item)
            fname = item.get("title") or (os.path.basename(fpath) if fpath else f"Message_{item.get('msg_id')}")
            chan_name = item.get("resolved_channel_title") or item.get("channel_title") or str(item.get("channel_id", ""))
            
            # 1. Channel Filter
            if selected_chan and selected_chan != "all":
                if str(item.get("channel_id")) != str(selected_chan):
                    continue

            # 2. Search text filter
            if search_text:
                match_name = search_text in fname.lower()
                match_id = search_text in str(item.get("msg_id", ""))
                match_path = search_text in (fpath.lower() if fpath else "")
                match_chan = search_text in chan_name.lower()
                if not (match_name or match_id or match_path or match_chan):
                    continue

            # 3. Category filter
            if cat_idx > 0:
                expected_cats = cat_map.get(cat_idx, [])
                item_cat = str(item.get("media_type", "")).lower()
                if item_cat not in expected_cats:
                    continue

            # 4. Status filter
            if status_idx == 1 and not on_disk: # Present on disk
                continue
            elif status_idx == 2 and on_disk: # Missing
                continue

            filtered.append((item, fname, chan_name, fpath, on_disk))
            if on_disk:
                total_disk_size += item.get("size") or (os.path.getsize(fpath) if (fpath and os.path.exists(fpath)) else 0)

        self.lbl_stats.setText(f"{len(filtered)} items ({format_bytes(total_disk_size)} on disk)")
        self.render_table(filtered)

    def render_table(self, filtered_items):
        self.table.setRowCount(0)
        self.table.setRowCount(len(filtered_items))

        type_icons = {
            "photo": "🖼️", "image": "🖼️", "images": "🖼️",
            "video": "🎬", "videos": "🎬",
            "pdf": "📄", "pdfs": "📄", "document": "📄", "documents": "📄",
            "zip": "📦", "zips": "📦",
            "audio": "🎵", "music": "🎵", "voice": "🎙️"
        }

        for row, (item, fname, chan_name, fpath, on_disk) in enumerate(filtered_items):
            # 0. Checkbox
            chk = QCheckBox()
            chk.setProperty("row_item", item)
            chk.stateChanged.connect(self.update_selected_count)
            chk_widget = QWidget()
            chk_layout = QHBoxLayout(chk_widget)
            chk_layout.setContentsMargins(8, 0, 0, 0)
            chk_layout.addWidget(chk)
            chk_layout.setAlignment(Qt.AlignCenter)
            self.table.setCellWidget(row, 0, chk_widget)

            # 1. Type Icon
            m_type = str(item.get("media_type", "")).lower()
            icon_str = type_icons.get(m_type, "📁")
            item_type = QTableWidgetItem(icon_str)
            item_type.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 1, item_type)

            # 2. File Name
            item_name = QTableWidgetItem(fname)
            item_name.setToolTip(fpath or fname)
            item_name.setData(Qt.UserRole, (item, fpath, on_disk))
            self.table.setItem(row, 2, item_name)

            # 3. Channel Name
            item_chan = QTableWidgetItem(chan_name)
            item_chan.setToolTip(f"Channel: {chan_name} (ID: {item.get('channel_id')})")
            self.table.setItem(row, 3, item_chan)

            # 4. Size
            size_bytes = item.get("size") or (os.path.getsize(fpath) if (on_disk and fpath and os.path.exists(fpath)) else 0)
            item_size = QTableWidgetItem(format_bytes(size_bytes))
            item_size.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.table.setItem(row, 4, item_size)

            # 5. Date
            date_str = str(item.get("date") or "")[:10]
            item_date = QTableWidgetItem(date_str)
            item_date.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(row, 5, item_date)

            # 6. Status Badge
            status_text = "🟢 On Disk" if on_disk else "🟡 Missing"
            item_status = QTableWidgetItem(status_text)
            item_status.setTextAlignment(Qt.AlignCenter)
            if not on_disk:
                item_status.setForeground(QColor("#F59E0B"))
            else:
                item_status.setForeground(QColor("#10B981"))
            self.table.setItem(row, 6, item_status)

            # 7. Action Buttons (Always visible & styled)
            action_widget = QWidget()
            act_layout = QHBoxLayout(action_widget)
            act_layout.setContentsMargins(2, 2, 2, 2)
            act_layout.setSpacing(4)

            if on_disk:
                btn_open = QPushButton("📂 Open")
                btn_open.setObjectName("CardButtonCompact")
                btn_open.setCursor(Qt.PointingHandCursor)
                btn_open.setToolTip("Open this file")
                btn_open.clicked.connect(lambda checked=False, p=fpath: self.open_file(p))
                act_layout.addWidget(btn_open)

                btn_folder = QPushButton("📁 Folder")
                btn_folder.setObjectName("CardButtonCompact")
                btn_folder.setCursor(Qt.PointingHandCursor)
                btn_folder.setToolTip("Show file in folder")
                btn_folder.clicked.connect(lambda checked=False, p=fpath: self.show_in_folder(p))
                act_layout.addWidget(btn_folder)
            else:
                btn_re = QPushButton("🔄 Re-fetch")
                btn_re.setObjectName("CardButtonCompact")
                btn_re.setCursor(Qt.PointingHandCursor)
                btn_re.setToolTip("Mark for re-download")
                btn_re.clicked.connect(lambda checked=False, it=item: self.redownload_item(it))
                act_layout.addWidget(btn_re)

            btn_del = QPushButton("🗑")
            btn_del.setObjectName("CardButtonCompact")
            btn_del.setCursor(Qt.PointingHandCursor)
            btn_del.setToolTip("Delete / Remove item")
            btn_del.clicked.connect(lambda checked=False, it=item, p=fpath, od=on_disk: self.prompt_delete_item(it, p, od))
            act_layout.addWidget(btn_del)

            self.table.setCellWidget(row, 7, action_widget)

        self.update_selected_count()

    def get_selected_rows_data(self):
        """Returns list of (item_dict, filepath, on_disk) for checked rows."""
        selected = []
        for row in range(self.table.rowCount()):
            cell_widget = self.table.cellWidget(row, 0)
            if cell_widget:
                chk = cell_widget.findChild(QCheckBox)
                if chk and chk.isChecked():
                    name_item = self.table.item(row, 2)
                    if name_item:
                        data = name_item.data(Qt.UserRole)
                        if data:
                            selected.append(data)
        return selected

    def toggle_select_all(self, state):
        checked = bool(state == Qt.Checked or state == 2)
        for row in range(self.table.rowCount()):
            cell_widget = self.table.cellWidget(row, 0)
            if cell_widget:
                chk = cell_widget.findChild(QCheckBox)
                if chk:
                    chk.setChecked(checked)
        self.update_selected_count()

    def update_selected_count(self):
        selected = self.get_selected_rows_data()
        self.lbl_selected_count.setText(f"{len(selected)} selected")
        has_sel = len(selected) > 0
        self.btn_bulk_remove_db.setEnabled(has_sel)
        self.btn_bulk_delete_disk.setEnabled(has_sel)

    def open_file(self, fpath):
        if fpath and os.path.exists(fpath):
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.abspath(fpath)))
        else:
            QMessageBox.warning(self, "File Not Found", f"The file does not exist on disk:\n{fpath}")

    def show_in_folder(self, fpath):
        if fpath and os.path.exists(fpath):
            if os.name == 'nt':
                subprocess.Popen(f'explorer /select,"{os.path.abspath(fpath)}"')
            else:
                folder = os.path.dirname(fpath)
                QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.abspath(folder)))
        elif fpath:
            folder = os.path.dirname(fpath)
            if os.path.exists(folder):
                QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.abspath(folder)))

    def open_downloads_folder(self):
        cfg = load_config()
        folder = cfg.get("download_path", "downloads")
        os.makedirs(folder, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.abspath(folder)))

    def on_row_double_click(self, index):
        row = index.row()
        name_item = self.table.item(row, 2)
        if name_item:
            data = name_item.data(Qt.UserRole)
            if data:
                item, fpath, on_disk = data
                if on_disk:
                    self.open_file(fpath)

    def redownload_item(self, item):
        c_id = item.get("channel_id")
        msg_id = item.get("msg_id")
        unmark_media_completed(c_id, msg_id)
        QMessageBox.information(
            self, "Marked for Re-download", 
            f"Message #{msg_id} was unmarked as completed.\nStart a download for this channel on the Home tab to re-download it!"
        )
        self.refresh_list()

    def prompt_delete_item(self, item, fpath, on_disk):
        c_id = item.get("channel_id")
        msg_id = item.get("msg_id")
        fname = item.get("title") or f"Message {msg_id}"

        msg_box = QMessageBox(self)
        msg_box.setWindowTitle("Delete Item")
        msg_box.setText(f"How would you like to delete '{fname}'?")
        
        btn_list_only = msg_box.addButton("Remove from List only", QMessageBox.ActionRole)
        btn_disk = None
        if on_disk and fpath and os.path.exists(fpath):
            btn_disk = msg_box.addButton("Delete from Disk & List", QMessageBox.DestructiveRole)
        btn_cancel = msg_box.addButton("Cancel", QMessageBox.RejectRole)

        msg_box.exec()
        clicked = msg_box.clickedButton()

        if clicked == btn_list_only:
            delete_media_cache_item(c_id, msg_id)
            self.refresh_list()
        elif clicked == btn_disk:
            try:
                if os.path.exists(fpath):
                    os.remove(fpath)
            except Exception as e:
                QMessageBox.warning(self, "Error", f"Failed to remove file from disk: {e}")
            delete_media_cache_item(c_id, msg_id)
            self.refresh_list()

    def bulk_remove_from_list(self):
        selected = self.get_selected_rows_data()
        if not selected: return

        reply = QMessageBox.question(
            self, "Remove from List",
            f"Remove {len(selected)} item(s) from the download history list?\n(Files on disk will NOT be deleted)",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            items_to_del = [(item.get("channel_id"), item.get("msg_id")) for item, _, _ in selected]
            delete_multiple_media_cache_items(items_to_del)
            self.refresh_list()

    def bulk_delete_from_disk(self):
        selected = self.get_selected_rows_data()
        if not selected: return

        reply = QMessageBox.warning(
            self, "Delete Files from Disk",
            f"Are you sure you want to permanently DELETE {len(selected)} file(s) from disk and remove them from history?\n\nThis action cannot be undone.",
            QMessageBox.Yes | QMessageBox.No
        )
        if reply == QMessageBox.Yes:
            items_to_del = []
            for item, fpath, on_disk in selected:
                if on_disk and fpath and os.path.exists(fpath):
                    try: os.remove(fpath)
                    except Exception as e:
                        print(f"Error deleting {fpath}: {e}")
                items_to_del.append((item.get("channel_id"), item.get("msg_id")))
            delete_multiple_media_cache_items(items_to_del)
            self.refresh_list()

    def show_context_menu(self, pos):
        item = self.table.itemAt(pos)
        if not item: return
        row = item.row()
        name_item = self.table.item(row, 2)
        if not name_item: return

        data = name_item.data(Qt.UserRole)
        if not data: return
        item_dict, fpath, on_disk = data

        menu = QMenu(self)
        if on_disk:
            action_open = menu.addAction("📂 Open File")
            action_folder = menu.addAction("📁 Show in File Explorer")
            action_copy_path = menu.addAction("📋 Copy Full Path")
            menu.addSeparator()
        else:
            action_open = None
            action_folder = None
            action_copy_path = None
            action_redownload = menu.addAction("🔄 Mark for Re-download")
            menu.addSeparator()

        action_copy_name = menu.addAction("📋 Copy File Name")
        menu.addSeparator()
        action_remove_db = menu.addAction("🗑 Remove from Download List (Keep on Disk)")
        
        if on_disk:
            action_delete_disk = menu.addAction("❌ Delete File from Disk & List")
        else:
            action_delete_disk = None

        action = menu.exec(self.table.mapToGlobal(pos))
        if not action: return

        if action == action_open and on_disk:
            self.open_file(fpath)
        elif action == action_folder and on_disk:
            self.show_in_folder(fpath)
        elif action == action_copy_path and fpath:
            QApplication.clipboard().setText(os.path.abspath(fpath))
        elif action == action_copy_name:
            fname = item_dict.get("title") or (os.path.basename(fpath) if fpath else "")
            QApplication.clipboard().setText(fname)
        elif action == action_remove_db:
            delete_media_cache_item(item_dict.get("channel_id"), item_dict.get("msg_id"))
            self.refresh_list()
        elif action == action_delete_disk and on_disk:
            self.prompt_delete_item(item_dict, fpath, on_disk)
        elif not on_disk and action == action_redownload:
            self.redownload_item(item_dict)
