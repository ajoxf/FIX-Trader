"""Native PySide6 desktop UI for the TT FIX trading terminal."""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableView,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ttfix_core import Instrument, Order, Quote, SessionSnapshot, SymbolMapping, Tick, TradingService


class EventBridge(QObject):
    event = Signal(str, object)


class DictTableModel(QAbstractTableModel):
    def __init__(self, columns: list[tuple[str, str]], max_rows: int = 0):
        super().__init__()
        self.columns = columns
        self.rows: list[dict[str, Any]] = []
        self.max_rows = max_rows

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.columns)

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        if not index.isValid() or not (0 <= index.row() < len(self.rows)):
            return None
        key = self.columns[index.column()][0]
        value = self.rows[index.row()].get(key, "")
        if role == Qt.DisplayRole:
            if value is None:
                return "—"
            if isinstance(value, float):
                return f"{value:g}"
            return str(value)
        if role == Qt.ForegroundRole:
            entry_type = self.rows[index.row()].get("entry_type")
            if entry_type == "BID":
                return QColor("#39d98a")
            if entry_type == "ASK":
                return QColor("#ff6b7a")
            if entry_type == "TRADE":
                return QColor("#62a8ff")
        if role == Qt.TextAlignmentRole and isinstance(value, (int, float)):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole) -> Any:  # noqa: N802
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return self.columns[section][1]
        return super().headerData(section, orientation, role)

    def replace(self, rows: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self.rows = list(rows)
        if self.max_rows:
            self.rows = self.rows[: self.max_rows]
        self.endResetModel()

    def prepend_many(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        incoming = rows[: self.max_rows] if self.max_rows else rows
        self.beginInsertRows(QModelIndex(), 0, len(incoming) - 1)
        self.rows[0:0] = incoming
        self.endInsertRows()
        if self.max_rows and len(self.rows) > self.max_rows:
            first = self.max_rows
            last = len(self.rows) - 1
            self.beginRemoveRows(QModelIndex(), first, last)
            del self.rows[first : last + 1]
            self.endRemoveRows()

    def upsert(self, key: str, row: dict[str, Any]) -> None:
        target = row.get(key)
        for position, current in enumerate(self.rows):
            if current.get(key) == target:
                self.rows[position] = row
                self.dataChanged.emit(self.index(position, 0), self.index(position, len(self.columns) - 1))
                return
        self.beginInsertRows(QModelIndex(), len(self.rows), len(self.rows))
        self.rows.append(row)
        self.endInsertRows()


def make_table(model: DictTableModel, *, sorting: bool = False) -> QTableView:
    table = QTableView()
    table.setModel(model)
    table.setAlternatingRowColors(True)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setSortingEnabled(sorting)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
    return table


class OverviewPanel(QWidget):
    def __init__(self, window: "DesktopWindow"):
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)
        metrics = QHBoxLayout()
        self.metric_labels: dict[str, QLabel] = {}
        for key, title in (
            ("market", "Market Data"), ("orders", "Order Routing"),
            ("ticks", "Ticks Received"), ("open", "Open Orders"), ("fills", "Executions"),
        ):
            box = QGroupBox(title)
            box_layout = QVBoxLayout(box)
            value = QLabel("—")
            value.setStyleSheet("font-size:22px;font-weight:700;color:#62a8ff")
            box_layout.addWidget(value)
            metrics.addWidget(box)
            self.metric_labels[key] = value
        layout.addLayout(metrics)
        layout.addWidget(QLabel("Latest market data"))
        self.quote_model = DictTableModel([
            ("symbol", "Symbol"), ("bid", "Bid"), ("bid_size", "Bid size"),
            ("ask", "Ask"), ("ask_size", "Ask size"), ("last", "Last"),
            ("last_size", "Last size"), ("timestamp", "Received UTC"),
        ])
        layout.addWidget(make_table(self.quote_model), 1)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(500)
        self.refresh()

    def refresh(self) -> None:
        service = self.window.service
        self.metric_labels["market"].setText(service.market_session.snapshot().status)
        self.metric_labels["orders"].setText(service.order_session.snapshot().status)
        self.metric_labels["ticks"].setText(f"{service.tick_count:,}")
        self.metric_labels["open"].setText(str(sum(
            order.status in {"PENDING", "NEW", "PARTIALLY_FILLED"}
            for order in service.orders.values()
        )))
        self.metric_labels["fills"].setText(str(len(service.execution_rows())))
        with service.data_lock:
            quotes = [asdict(quote) for quote in service.quotes.values()]
        self.quote_model.replace(quotes)


class ConnectionPanel(QWidget):
    def __init__(self, window: "DesktopWindow"):
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)
        self.cards: dict[str, dict[str, QLabel]] = {}
        for name, title in (("MarketData", "Market Data FIX"), ("OrderRouting", "Order Routing FIX")):
            group = QGroupBox(title)
            grid = QGridLayout(group)
            labels = {
                "status": QLabel("DISCONNECTED"), "ids": QLabel("—"),
                "messages": QLabel("IN/OUT 0/0"), "sequence": QLabel("SEQ 0/0"),
                "heartbeat": QLabel("Last heartbeat: —"), "error": QLabel(""),
            }
            labels["status"].setObjectName("statusLabel")
            labels["error"].setStyleSheet("color:#ff6b7a")
            grid.addWidget(labels["status"], 0, 0)
            grid.addWidget(labels["ids"], 0, 1)
            grid.addWidget(labels["messages"], 1, 0)
            grid.addWidget(labels["sequence"], 1, 1)
            grid.addWidget(labels["heartbeat"], 2, 0, 1, 2)
            grid.addWidget(labels["error"], 3, 0, 1, 2)
            controls = QHBoxLayout()
            connect = QPushButton("Connect")
            disconnect = QPushButton("Disconnect")
            reconnect = QPushButton("Reconnect")
            controls.addWidget(connect)
            controls.addWidget(disconnect)
            controls.addWidget(reconnect)
            grid.addLayout(controls, 4, 0, 1, 2)
            if name == "MarketData":
                connect.clicked.connect(window.connect_market)
                disconnect.clicked.connect(window.service.disconnect_market)
                reconnect.clicked.connect(window.reconnect_market)
            else:
                connect.clicked.connect(window.connect_orders)
                disconnect.clicked.connect(window.service.disconnect_orders)
                reconnect.clicked.connect(window.reconnect_orders)
            self.cards[name] = labels
            layout.addWidget(group)
        layout.addStretch()

    def update_state(self, state: SessionSnapshot) -> None:
        labels = self.cards[state.name]
        labels["status"].setText(state.status)
        color = "#39d98a" if state.status == "CONNECTED" else "#f8c451" if state.status == "CONNECTING" else "#ff6b7a"
        labels["status"].setStyleSheet(f"font-weight:700;color:{color}")
        labels["ids"].setText(f"{state.sender_comp_id} → {state.target_comp_id}")
        labels["messages"].setText(f"IN/OUT {state.incoming_count}/{state.outgoing_count}")
        labels["sequence"].setText(f"SEQ {state.in_seq}/{state.out_seq}")
        labels["heartbeat"].setText(f"Last heartbeat: {state.last_heartbeat or '—'}")
        labels["error"].setText(state.error)


class MarketDataPanel(QWidget):
    def __init__(self, window: "DesktopWindow"):
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)
        request_box = QGroupBox("Market-data subscription")
        request = QGridLayout(request_box)
        self.exchange = QLineEdit("CME")
        self.symbol = QLineEdit("BZ")
        self.security_id = QLineEdit()
        self.security_type = QLineEdit("FUT")
        self.maturity = QLineEdit()
        self.full_book = QCheckBox("Full book")
        self.full_book.setChecked(False)
        self.full_book.setToolTip("Top-of-book is faster. Enable this only when full depth is required.")
        self.continuous = QCheckBox("Snapshot + live updates")
        self.continuous.setChecked(True)
        fields = [
            ("Exchange", self.exchange), ("Symbol", self.symbol),
            ("TT Security ID", self.security_id), ("Security type", self.security_type),
            ("Contract month", self.maturity),
        ]
        for index, (label, widget) in enumerate(fields):
            request.addWidget(QLabel(label), (index // 3) * 2, index % 3)
            request.addWidget(widget, (index // 3) * 2 + 1, index % 3)
        request.addWidget(self.full_book, 4, 0)
        request.addWidget(self.continuous, 4, 1)
        subscribe = QPushButton("Subscribe")
        unsubscribe = QPushButton("Unsubscribe")
        subscribe.setObjectName("primaryButton")
        request.addWidget(subscribe, 4, 2)
        request.addWidget(unsubscribe, 5, 2)
        subscribe.clicked.connect(self.subscribe)
        unsubscribe.clicked.connect(self.unsubscribe)
        layout.addWidget(request_box)

        self.status = QLabel("Event-driven feed · waiting for data")
        layout.addWidget(self.status)
        self.response_timer = QTimer(self)
        self.response_timer.setSingleShot(True)
        self.response_timer.setInterval(5000)
        self.response_timer.timeout.connect(self.response_timeout)
        self.pending_symbol = ""
        self.pending_request_id = ""
        self.request_started = 0.0
        self.quote_model = DictTableModel([
            ("symbol", "Symbol"), ("bid", "Bid"), ("bid_size", "Bid size"),
            ("ask", "Ask"), ("ask_size", "Ask size"), ("last", "Last"),
            ("last_size", "Last size"), ("timestamp", "Received UTC"),
        ])
        layout.addWidget(make_table(self.quote_model))
        layout.addWidget(QLabel("Real-time tick tape"))
        self.tick_model = DictTableModel([
            ("received_at", "Received UTC"), ("exchange_time", "Exchange time"),
            ("sequence", "Seq"), ("symbol", "Symbol"), ("update", "Update"),
            ("entry_type", "Type"), ("price", "Price"), ("size", "Size"),
            ("entry_id", "Entry ID"), ("position", "Position"),
        ], max_rows=5000)
        self.tick_table = make_table(self.tick_model)
        layout.addWidget(self.tick_table, 1)

    def subscribe(self) -> None:
        try:
            self.window.service.subscribe_market_data(
                symbol=self.symbol.text().strip(), exchange=self.exchange.text().strip(),
                security_id=self.security_id.text().strip(),
                security_type=self.security_type.text().strip(), maturity=self.maturity.text().strip(),
                full_book=self.full_book.isChecked(), continuous=self.continuous.isChecked(),
            )
        except Exception as exc:
            self.window.show_error(str(exc))

    def unsubscribe(self) -> None:
        try:
            self.window.service.unsubscribe_market_data(self.symbol.text().strip())
        except Exception as exc:
            self.window.show_error(str(exc))

    def apply_batch(self, batch: list[tuple[Tick, Quote]]) -> None:
        ticks = [asdict(tick) for tick, _ in reversed(batch)]
        self.tick_model.prepend_many(ticks)
        latest: dict[str, Quote] = {}
        for _, quote in batch:
            latest[quote.symbol] = quote
        for quote in latest.values():
            self.quote_model.upsert("symbol", asdict(quote))
        latency = ""
        if self.pending_symbol and any(tick.symbol == self.pending_symbol for tick, _ in batch):
            self.response_timer.stop()
            latency = f" · first response in {time.monotonic() - self.request_started:.2f}s"
            self.pending_symbol = ""
            self.pending_request_id = ""
        self.status.setText(
            f"Live feed · {self.window.service.tick_count:,} entries received · "
            f"{len(self.tick_model.rows):,} displayed{latency}"
        )

    def request_sent(self, subscription: dict[str, Any]) -> None:
        self.pending_symbol = str(subscription["symbol"])
        self.pending_request_id = str(subscription["request_id"])
        self.request_started = time.monotonic()
        self.response_timer.start()
        mode = "live updates" if subscription["continuous"] else "snapshot"
        depth = "full book" if subscription["full_book"] else "top of book"
        self.status.setText(
            f"Subscription sent for {self.pending_symbol} ({mode}, {depth}) · waiting for TT"
        )

    def response_received(self, payload: dict[str, str]) -> None:
        if not self.pending_symbol:
            return
        request_id = payload.get("request_id", "")
        if request_id and request_id != self.pending_request_id:
            return
        elapsed = time.monotonic() - self.request_started
        self.response_timer.stop()
        symbol = payload.get("symbol") or self.pending_symbol
        message_name = "snapshot" if payload.get("message_type") == "W" else "update"
        self.pending_symbol = ""
        self.pending_request_id = ""
        self.status.setText(
            f"TT {message_name} received for {symbol} in {elapsed:.2f}s · no price entries in response"
        )

    def response_timeout(self) -> None:
        if not self.pending_symbol:
            return
        self.status.setText(
            f"No TT response for {self.pending_symbol} after 5s · check Executions and logs "
            "for 35=Y, 35=j, or 35=3; verify instrument and market-data entitlement"
        )


class InstrumentPanel(QWidget):
    def __init__(self, window: "DesktopWindow"):
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)
        controls = QHBoxLayout()
        self.exchange = QLineEdit("CME")
        self.security_type = QComboBox()
        self.security_type.addItems(["FUT", "OPT", "MLEG", "CS", "CUR", "FOR", "SPOT"])
        self.symbol = QLineEdit("BZ")
        self.maturity = QLineEdit()
        search = QPushButton("Fetch instruments")
        search.setObjectName("primaryButton")
        for label, widget in (("Exchange", self.exchange), ("Type", self.security_type), ("Symbol", self.symbol), ("Month", self.maturity)):
            controls.addWidget(QLabel(label))
            controls.addWidget(widget)
        controls.addWidget(search)
        search.clicked.connect(self.search)
        layout.addLayout(controls)
        self.model = DictTableModel([
            ("security_id", "Security ID"), ("symbol", "Symbol"), ("exchange", "Exchange"),
            ("security_type", "Type"), ("maturity_month_year", "Month"),
            ("description", "Description"), ("currency", "Currency"),
        ])
        self.table = make_table(self.model)
        layout.addWidget(self.table)
        use_market = QPushButton("Use selected instrument in Market Data")
        use_order = QPushButton("Use selected instrument in Order Entry")
        row = QHBoxLayout()
        row.addWidget(use_market)
        row.addWidget(use_order)
        layout.addLayout(row)
        use_market.clicked.connect(lambda: self.use_selected("market"))
        use_order.clicked.connect(lambda: self.use_selected("order"))

    def search(self) -> None:
        try:
            self.model.replace([])
            self.window.service.request_instruments(
                self.exchange.text().strip(), self.security_type.currentText(),
                self.symbol.text().strip(), self.maturity.text().strip(),
            )
        except Exception as exc:
            self.window.show_error(str(exc))

    def add_instrument(self, instrument: Instrument) -> None:
        self.model.upsert("security_id", asdict(instrument))

    def selected(self) -> dict[str, Any] | None:
        indexes = self.table.selectionModel().selectedRows()
        return self.model.rows[indexes[0].row()] if indexes else None

    def use_selected(self, target: str) -> None:
        item = self.selected()
        if not item:
            self.window.show_error("Select an instrument first")
            return
        if target == "market":
            panel = self.window.market_panel
        else:
            panel = self.window.order_panel
        panel.symbol.setText(str(item["symbol"]))
        panel.exchange.setText(str(item["exchange"]))
        panel.security_id.setText(str(item["security_id"]))
        panel.security_type.setText(str(item["security_type"]))
        panel.maturity.setText(str(item["maturity_month_year"]))
        self.window.tabs.setCurrentWidget(panel)


class OrderPanel(QWidget):
    def __init__(self, window: "DesktopWindow"):
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)
        form_box = QGroupBox("New order")
        form = QGridLayout(form_box)
        self.account = QLineEdit(str(window.service.config["order"]["account"]))
        self.exchange = QLineEdit()
        self.symbol = QLineEdit("ES")
        self.security_id = QLineEdit()
        self.security_type = QLineEdit("FUT")
        self.maturity = QLineEdit()
        self.side = QComboBox(); self.side.addItems(["BUY", "SELL"])
        self.order_type = QComboBox(); self.order_type.addItems(["MARKET", "LIMIT", "STOP", "STOP_LIMIT"])
        self.quantity = QDoubleSpinBox(); self.quantity.setRange(0, 1_000_000_000); self.quantity.setValue(1)
        self.price = QDoubleSpinBox(); self.price.setRange(0, 1_000_000_000); self.price.setDecimals(10)
        self.tif = QComboBox(); self.tif.addItems(["DAY", "GTC", "IOC", "FOK", "GTD"])
        widgets = [
            ("Account", self.account), ("Exchange", self.exchange), ("Symbol", self.symbol),
            ("Security ID", self.security_id), ("Security type", self.security_type), ("Month", self.maturity),
            ("Side", self.side), ("Order type", self.order_type), ("Quantity", self.quantity),
            ("Price", self.price), ("Time in force", self.tif),
        ]
        for index, (label, widget) in enumerate(widgets):
            row, column = divmod(index, 3)
            form.addWidget(QLabel(label), row * 2, column)
            form.addWidget(widget, row * 2 + 1, column)
        submit = QPushButton("Review and submit order")
        submit.setObjectName("dangerButton")
        form.addWidget(submit, 8, 2)
        submit.clicked.connect(self.submit)
        layout.addWidget(form_box)
        self.model = DictTableModel([
            ("client_order_id", "Client order ID"), ("symbol", "Symbol"), ("side", "Side"),
            ("order_type", "Type"), ("quantity", "Qty"), ("price", "Price"),
            ("status", "Status"), ("filled_qty", "Filled"),
            ("remaining_qty", "Remaining"), ("exchange_order_id", "TT order ID"),
            ("updated_at", "Updated UTC"),
        ])
        self.table = make_table(self.model)
        self.model.replace([asdict(order) for order in reversed(list(window.service.orders.values()))])
        layout.addWidget(self.table, 1)
        buttons = QHBoxLayout()
        cancel = QPushButton("Cancel selected")
        replace = QPushButton("Replace selected with form quantity/price")
        refresh = QPushButton("Refresh orders")
        buttons.addWidget(cancel); buttons.addWidget(replace); buttons.addWidget(refresh)
        layout.addLayout(buttons)
        cancel.clicked.connect(self.cancel)
        replace.clicked.connect(self.replace)
        refresh.clicked.connect(self.refresh)

    def submit(self) -> None:
        summary = (
            f"{self.side.currentText()} {self.quantity.value():g} {self.symbol.text()} "
            f"{self.order_type.currentText()}"
        )
        if self.price.value():
            summary += f" @ {self.price.value():g}"
        if QMessageBox.question(self, "Confirm live order", f"Submit this order?\n\n{summary}") != QMessageBox.Yes:
            return
        try:
            self.window.service.submit_order(
                account=self.account.text().strip(), symbol=self.symbol.text().strip(),
                side=self.side.currentText(), order_type=self.order_type.currentText(),
                quantity=self.quantity.value(), price=self.price.value() or None,
                tif=self.tif.currentText(), security_id=self.security_id.text().strip(),
                exchange=self.exchange.text().strip(), security_type=self.security_type.text().strip(),
                maturity=self.maturity.text().strip(),
            )
        except Exception as exc:
            self.window.show_error(str(exc))

    def selected_order_id(self) -> str | None:
        indexes = self.table.selectionModel().selectedRows()
        return str(self.model.rows[indexes[0].row()]["client_order_id"]) if indexes else None

    def cancel(self) -> None:
        order_id = self.selected_order_id()
        if not order_id:
            self.window.show_error("Select an order first")
            return
        if QMessageBox.question(self, "Confirm cancel", f"Cancel {order_id}?") == QMessageBox.Yes:
            try:
                self.window.service.cancel_order(order_id)
            except Exception as exc:
                self.window.show_error(str(exc))

    def replace(self) -> None:
        order_id = self.selected_order_id()
        if not order_id:
            self.window.show_error("Select an order first")
            return
        if QMessageBox.question(self, "Confirm replace", f"Replace {order_id} using the form quantity and price?") == QMessageBox.Yes:
            try:
                self.window.service.replace_order(order_id, self.quantity.value(), self.price.value() or None)
            except Exception as exc:
                self.window.show_error(str(exc))

    def update_order(self, order: Order) -> None:
        self.model.upsert("client_order_id", asdict(order))

    def refresh(self) -> None:
        self.model.replace([asdict(order) for order in reversed(list(self.window.service.orders.values()))])


class MappingPanel(QWidget):
    def __init__(self, window: "DesktopWindow"):
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)
        self.model = DictTableModel([
            ("bridge_symbol", "Bridge symbol"), ("maker", "Maker"),
            ("maker_symbol", "Maker symbol"), ("tt_symbol", "TT symbol"),
            ("tt_exchange", "TT exchange"), ("tt_security_id", "Security ID"),
            ("security_type", "Type"), ("price_digits", "Digits"),
            ("enabled", "Enabled"), ("updated_at", "Updated UTC"),
        ])
        self.model.replace(window.service.mapping_rows())
        layout.addWidget(make_table(self.model))
        form = QFormLayout()
        self.bridge = QLineEdit(); self.maker = QLineEdit(); self.maker_symbol = QLineEdit()
        self.tt_symbol = QLineEdit(); self.exchange = QLineEdit(); self.security_id = QLineEdit()
        self.security_type = QLineEdit(); self.digits = QSpinBox(); self.digits.setRange(0, 12); self.digits.setValue(5)
        self.enabled = QCheckBox(); self.enabled.setChecked(True)
        for label, widget in (
            ("Bridge symbol", self.bridge), ("Maker", self.maker), ("Maker symbol", self.maker_symbol),
            ("TT symbol", self.tt_symbol), ("TT exchange", self.exchange), ("TT Security ID", self.security_id),
            ("Security type", self.security_type), ("Price digits", self.digits), ("Enabled", self.enabled),
        ):
            form.addRow(label, widget)
        save = QPushButton("Save mapping"); save.setObjectName("primaryButton")
        save.clicked.connect(self.save)
        form.addRow(save)
        layout.addLayout(form)

    def save(self) -> None:
        try:
            mapping = SymbolMapping(
                bridge_symbol=self.bridge.text().strip().upper(), maker=self.maker.text().strip(),
                maker_symbol=self.maker_symbol.text().strip(), tt_symbol=self.tt_symbol.text().strip(),
                tt_exchange=self.exchange.text().strip(), tt_security_id=self.security_id.text().strip(),
                security_type=self.security_type.text().strip(), price_digits=self.digits.value(),
                enabled=self.enabled.isChecked(),
            )
            if not all((mapping.bridge_symbol, mapping.maker, mapping.maker_symbol, mapping.tt_symbol)):
                raise ValueError("Bridge symbol, maker, maker symbol, and TT symbol are required")
            self.window.service.save_mapping(mapping)
            self.model.upsert("bridge_symbol", asdict(mapping))
        except Exception as exc:
            self.window.show_error(str(exc))


class RecordsPanel(QWidget):
    def __init__(self, window: "DesktopWindow"):
        super().__init__()
        layout = QVBoxLayout(self)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self.execution_model = DictTableModel([
            ("execution_id", "Execution ID"), ("client_order_id", "Client order ID"),
            ("exchange_order_id", "TT order ID"), ("symbol", "Symbol"), ("side", "Side"),
            ("quantity", "Quantity"), ("price", "Price"), ("execution_status", "Status"),
            ("timestamp", "Timestamp"),
        ])
        self.execution_model.replace(window.service.execution_rows())
        executions = QWidget(); ex_layout = QVBoxLayout(executions)
        ex_refresh = QPushButton("Refresh executions")
        ex_refresh.clicked.connect(lambda: self.execution_model.replace(window.service.execution_rows()))
        ex_layout.addWidget(ex_refresh); ex_layout.addWidget(make_table(self.execution_model))
        self.tabs.addTab(executions, "Executions")
        self.log_model = DictTableModel([
            ("timestamp", "Timestamp"), ("session", "Session"), ("direction", "Direction"),
            ("message_type", "Msg type"), ("sequence", "Seq"), ("raw", "Raw FIX"),
        ], max_rows=5000)
        self.log_model.replace(list(reversed(window.service.logs)))
        self.tabs.addTab(make_table(self.log_model), "FIX log")

    def add_log(self, item: dict[str, str]) -> None:
        self.log_model.prepend_many([item])


class DiagnosticsPanel(QWidget):
    def __init__(self, window: "DesktopWindow"):
        super().__init__()
        self.window = window
        layout = QVBoxLayout(self)
        warning = QLabel(
            "Local checks only. Passing these checks does not constitute Trading Technologies certification."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color:#f8c451;font-weight:600")
        layout.addWidget(warning)
        refresh = QPushButton("Run diagnostics")
        refresh.clicked.connect(self.refresh)
        layout.addWidget(refresh)
        self.model = DictTableModel([
            ("check", "Check"), ("result", "Result"), ("details", "Details"),
        ])
        layout.addWidget(make_table(self.model))
        self.refresh()

    def refresh(self) -> None:
        service = self.window.service
        market = service.market_session.snapshot()
        orders = service.order_session.snapshot()
        config_ok = all(
            service.config[section].get(field)
            for section in ("order", "market_data")
            for field in ("host", "port", "sender_comp_id", "target_comp_id", "password")
        ) or service.config["mock_mode"]
        checks = [
            ("Configuration", config_ok, "Required session fields are present"),
            ("Market Data connection", market.status == "CONNECTED", market.status),
            ("Order Routing connection", orders.status == "CONNECTED", orders.status),
            ("Market Data logon", bool(market.last_logon), market.last_logon or "Not observed"),
            ("Order Routing logon", bool(orders.last_logon), orders.last_logon or "Not observed"),
            ("Heartbeat", bool(market.last_heartbeat or orders.last_heartbeat), market.last_heartbeat or orders.last_heartbeat or "Not observed"),
            ("Market-data ticks", service.tick_count > 0, f"{service.tick_count:,} entries"),
            ("Orders", bool(service.orders), f"{len(service.orders):,} stored orders"),
            ("Executions", bool(service.execution_rows()), f"{len(service.execution_rows()):,} stored executions"),
        ]
        self.model.replace([
            {"check": name, "result": "PASS" if passed else "FAIL", "details": details}
            for name, passed, details in checks
        ])


class DesktopWindow(QMainWindow):
    def __init__(self, service: TradingService):
        super().__init__()
        self.service = service
        self.setWindowTitle("TT FIX Desktop Terminal")
        self.resize(1500, 900)
        self.bridge = EventBridge()
        self.bridge.event.connect(self.on_event)
        self.service.add_listener(lambda event, payload: self.bridge.event.emit(event, payload))
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        self.overview_panel = OverviewPanel(self)
        self.connection_panel = ConnectionPanel(self)
        self.instrument_panel = InstrumentPanel(self)
        self.market_panel = MarketDataPanel(self)
        self.order_panel = OrderPanel(self)
        self.mapping_panel = MappingPanel(self)
        self.records_panel = RecordsPanel(self)
        self.diagnostics_panel = DiagnosticsPanel(self)
        self.tabs.addTab(self.overview_panel, "Dashboard")
        self.tabs.addTab(self.connection_panel, "Connectivity")
        self.tabs.addTab(self.instrument_panel, "Instrument lookup")
        self.tabs.addTab(self.market_panel, "Market data")
        self.tabs.addTab(self.order_panel, "Orders")
        self.tabs.addTab(self.mapping_panel, "Symbol bridge")
        self.tabs.addTab(self.records_panel, "Executions and logs")
        self.tabs.addTab(self.diagnostics_panel, "Diagnostics")
        settings = QTextEdit()
        settings.setReadOnly(True)
        safe_config = json.loads(json.dumps(service.config))
        safe_config["order"]["password"] = "****" if safe_config["order"].get("password") else ""
        safe_config["market_data"]["password"] = "****" if safe_config["market_data"].get("password") else ""
        settings.setPlainText(json.dumps(safe_config, indent=2))
        self.tabs.addTab(settings, "Settings")
        self.statusBar().showMessage(
            f"{service.config['environment']} · Native Python FIX · Event-driven PySide6 UI"
        )
        self.connection_panel.update_state(service.market_session.snapshot())
        self.connection_panel.update_state(service.order_session.snapshot())

    def show_error(self, message: str) -> None:
        QMessageBox.critical(self, "TT FIX", message)

    def connect_market(self) -> None:
        try:
            self.service.connect_market()
        except Exception as exc:
            self.show_error(str(exc))

    def connect_orders(self) -> None:
        try:
            self.service.connect_orders()
        except Exception as exc:
            self.show_error(str(exc))

    def reconnect_market(self) -> None:
        self.service.disconnect_market()
        QTimer.singleShot(1000, self.connect_market)

    def reconnect_orders(self) -> None:
        self.service.disconnect_orders()
        QTimer.singleShot(1000, self.connect_orders)

    def on_event(self, event: str, payload: Any) -> None:
        if event == "session":
            self.connection_panel.update_state(payload)
        elif event == "market_batch":
            self.market_panel.apply_batch(payload)
        elif event == "instrument":
            self.instrument_panel.add_instrument(payload)
        elif event == "order":
            self.order_panel.update_order(payload)
        elif event == "execution":
            self.records_panel.execution_model.replace(self.service.execution_rows())
        elif event == "log":
            self.records_panel.add_log(payload)
        elif event == "market_reject":
            self.market_panel.response_timer.stop()
            self.market_panel.pending_symbol = ""
            self.market_panel.pending_request_id = ""
            self.market_panel.status.setText(f"Market-data request rejected: {payload}")
            self.show_error(str(payload))
        elif event == "market_response":
            self.market_panel.response_received(payload)
        elif event == "market_subscription":
            self.market_panel.request_sent(payload)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.service.close()
        event.accept()


STYLE = """
QWidget { background:#000000; color:#eeeeee; font-family:'Segoe UI'; font-size:12px; }
QMainWindow, QTabWidget::pane { background:#000000; }
QTabBar::tab { background:#080808; color:#aaaaaa; padding:10px 18px; border:1px solid #242424; }
QTabBar::tab:selected { background:#181818; color:#ffffff; border-bottom:2px solid #3d8bfd; }
QTabBar::tab:hover { background:#111111; color:#ffffff; }
QGroupBox { border:1px solid #292929; border-radius:8px; margin-top:12px; padding:12px; font-weight:700; }
QGroupBox::title { subcontrol-origin:margin; left:12px; padding:0 5px; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit {
    background:#0b0b0b; border:1px solid #333333; border-radius:5px; padding:6px; color:#f5f5f5;
}
QPushButton { background:#151515; border:1px solid #383838; border-radius:6px; padding:7px 13px; font-weight:600; }
QPushButton:hover { border-color:#6aa6ff; background:#202020; }
QPushButton#primaryButton { background:#2673ee; color:white; border:none; }
QPushButton#dangerButton { background:#9e3045; color:white; border:none; }
QTableView { background:#030303; alternate-background-color:#090909; border:1px solid #292929; gridline-color:#222222; }
QHeaderView::section { background:#111111; color:#b8b8b8; padding:7px; border:0; border-right:1px solid #292929; }
QTableView::item:selected { background:#234e78; color:white; }
QScrollBar:vertical { background:#000000; width:10px; }
QScrollBar::handle:vertical { background:#353535; border-radius:5px; }
QToolTip { background:#151515; color:#ffffff; border:1px solid #444444; }
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="TT FIX PySide6 desktop terminal")
    parser.add_argument("--smoke-test", action="store_true", help="Create the window and exit automatically")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("TT FIX Desktop")
    app.setStyleSheet(STYLE)
    app.setFont(QFont("Segoe UI", 10))
    service = TradingService()
    window = DesktopWindow(service)
    if args.smoke_test:
        def finish_smoke_test() -> None:
            window.close()
            app.quit()

        QTimer.singleShot(250, finish_smoke_test)
    else:
        window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
