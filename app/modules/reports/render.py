# pyright: basic
# The rendering libraries ship no type information; this module only turns a
# report value into bytes, so strict typing would add casts without safety.
"""Trial balance rendering: Arabic-aware PDF and Excel (phase 0, day 8).

The PDF shapes Arabic with arabic-reshaper + python-bidi and embeds a system
Arabic font when one is available; without a font it still produces a valid PDF
using the built-in font (Latin text renders, Arabic may not shape). The Excel
workbook marks its sheet right-to-left so Arabic columns read naturally.
"""

from __future__ import annotations

import io
import json
import os
from collections.abc import Iterable
from decimal import Decimal
from typing import cast
from uuid import UUID

from arabic_reshaper import reshape
from bidi.algorithm import get_display
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFError, TTFont
from reportlab.platypus import (
    Flowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.modules.reports.service import (
    AgingReport,
    AuditTrail,
    BalanceSheet,
    DocumentRegister,
    GeneralLedger,
    InventoryValuation,
    PartyStatement,
    ProfitAndLoss,
    StatementRow,
    StockMovementReport,
    TaxReturn,
    TrialBalance,
)

PDF_FONT_CANDIDATES = (
    # An explicit override wins everywhere (container images, locked-down hosts).
    os.environ.get("MAHMOUD_PDF_FONT", ""),
    # Windows hosts.
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\tahoma.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    # Debian/Ubuntu images and CI runners (fonts-dejavu-core, fonts-noto-core).
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
    "/usr/share/fonts/truetype/noto/NotoNaskhArabicUI-Regular.ttf",
)
FONT_NAME = "MahmoudArabic"
_registered_font: str | None = None


def _font() -> str:
    """Register the first available Arabic-capable font once; else use Helvetica."""
    global _registered_font
    if _registered_font is not None:
        return _registered_font
    for path in PDF_FONT_CANDIDATES:
        if not path:
            continue
        try:
            pdfmetrics.registerFont(TTFont(FONT_NAME, path))
        except OSError, ValueError, KeyError, TTFError:
            # A missing or unreadable font is not fatal: the next candidate gets
            # a turn, and Helvetica is a valid Latin fallback. This is what lets
            # the same image run on a Windows host and in a Linux container.
            continue
        _registered_font = FONT_NAME
        return FONT_NAME
    _registered_font = "Helvetica"
    return _registered_font


def arabic(text: str) -> str:
    """Shape and reorder one Arabic string for a simple left-to-right renderer."""
    return cast(str, get_display(reshape(text)))


def _amount(minor: int) -> str:
    value = Decimal(minor) / Decimal(100)
    return f"{value:,.2f}"


def _p(text: str, font: str, *, size: int = 9, bold: bool = False) -> Paragraph:
    style = ParagraphStyle(
        name="MahmoudCell",
        fontName=font,
        fontSize=size,
        leading=size + 3,
        alignment=1,
        textColor=colors.black,
        wordWrap="CJK",
    )
    prefix = "<b>" if bold else ""
    suffix = "</b>" if bold else ""
    return Paragraph(f"{prefix}{text}{suffix}", style)


def render_trial_balance_pdf(
    balance: TrialBalance,
    *,
    company_name_ar: str,
    company_name_en: str | None = None,
    comparison: TrialBalance | None = None,
    meta_lines: list[str] | None = None,
) -> bytes:
    """Render the trial balance as an A4 landscape PDF with Arabic headers.

    ``comparison`` adds the prior period's debit, credit and balance columns so
    a printed trial balance carries the same comparison the JSON view offers.
    """
    font = _font()
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title="Trial Balance",
    )
    title = ParagraphStyle(name="MahmoudTitle", fontName=font, fontSize=16, leading=20, alignment=1)
    subtitle = ParagraphStyle(
        name="MahmoudSubtitle", fontName=font, fontSize=10, leading=14, alignment=1
    )
    story: list[Flowable] = [
        Paragraph(arabic("ميزان المراجعة"), title),
        Paragraph(arabic(company_name_ar), subtitle),
    ]
    if company_name_en:
        story.append(Paragraph(company_name_en, subtitle))
    period = f"{balance.start_date.isoformat()} - {balance.end_date.isoformat()}"
    story.append(Paragraph(period, subtitle))
    comparing = comparison is not None
    if comparison is not None:
        comparison_period = (
            f"{comparison.start_date.isoformat()} - {comparison.end_date.isoformat()}"
        )
        story.append(Paragraph(arabic(f"فترة المقارنة: {comparison_period}"), subtitle))
    for line in meta_lines or []:
        story.append(Paragraph(arabic(line), subtitle))
    story.append(Spacer(1, 6 * mm))

    prior = {row.account_id: row for row in comparison.rows} if comparison is not None else {}
    headers = [
        arabic("رمز الحساب"),
        arabic("اسم الحساب"),
        arabic("مدين"),
        arabic("دائن"),
        arabic("الرصيد"),
    ]
    if comparing:
        headers.extend([arabic("مدين سابق"), arabic("دائن سابق"), arabic("الرصيد السابق")])
    data: list[list[object]] = [[_p(header, font, bold=True) for header in headers]]
    for row in balance.rows:
        name = row.name_ar if company_name_en is None else (row.name_en or row.name_ar)
        cells: list[object] = [
            _p(row.code, font),
            _p(arabic(name), font),
            _p(_amount(row.debit_minor), font),
            _p(_amount(row.credit_minor), font),
            _p(_amount(row.balance_minor), font),
        ]
        if comparing:
            previous = prior.get(row.account_id)
            cells.extend(
                [
                    _p(_amount(previous.debit_minor if previous is not None else 0), font),
                    _p(_amount(previous.credit_minor if previous is not None else 0), font),
                    _p(_amount(previous.balance_minor if previous is not None else 0), font),
                ]
            )
        data.append(cells)
    totals: list[object] = [
        _p("", font),
        _p(arabic("الإجمالي"), font, bold=True),
        _p(_amount(balance.total_debit_minor), font, bold=True),
        _p(_amount(balance.total_credit_minor), font, bold=True),
        _p(_amount(balance.total_debit_minor - balance.total_credit_minor), font, bold=True),
    ]
    if comparing and comparison is not None:
        totals.extend(
            [
                _p(_amount(comparison.total_debit_minor), font, bold=True),
                _p(_amount(comparison.total_credit_minor), font, bold=True),
                _p(
                    _amount(comparison.total_debit_minor - comparison.total_credit_minor),
                    font,
                    bold=True,
                ),
            ]
        )
    data.append(totals)
    widths = (
        [20 * mm, 60 * mm, 27 * mm, 27 * mm, 27 * mm, 27 * mm, 27 * mm, 27 * mm]
        if comparing
        else [30 * mm, 90 * mm, 45 * mm, 45 * mm, 45 * mm]
    )
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(arabic("أنشئ بواسطة المحمود"), subtitle))
    document.build(story)
    return buffer.getvalue()


def render_trial_balance_xlsx(
    balance: TrialBalance,
    *,
    company_name_ar: str,
    company_name_en: str | None = None,
    comparison: TrialBalance | None = None,
    meta_lines: list[str] | None = None,
) -> bytes:
    """Render the trial balance as a right-to-left Excel workbook."""
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "ميزان المراجعة"
    sheet.sheet_view.rightToLeft = True
    header_font = Font(bold=True, size=12)
    comparing = comparison is not None
    column_count = 8 if comparing else 5
    sheet.append([company_name_ar, company_name_en or ""])
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=column_count)
    sheet.cell(row=1, column=1).font = Font(bold=True, size=14)
    sheet.cell(row=1, column=1).alignment = Alignment(horizontal="center")
    sheet.append([f"{balance.start_date.isoformat()} - {balance.end_date.isoformat()}"])
    sheet.merge_cells(start_row=2, start_column=1, end_row=2, end_column=column_count)
    sheet.append([])
    for line in meta_lines or []:
        sheet.append([line])
        sheet.merge_cells(
            start_row=sheet.max_row, start_column=1, end_row=sheet.max_row, end_column=column_count
        )
    header_row = 3 + len(meta_lines or []) + 1
    headers = ["رمز الحساب", "اسم الحساب", "مدين", "دائن", "الرصيد"]
    if comparing:
        headers.extend(["مدين سابق", "دائن سابق", "الرصيد السابق"])
    for column, header in enumerate(headers, start=1):
        cell = sheet.cell(row=header_row, column=column, value=header)
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")
    prior = {row.account_id: row for row in comparison.rows} if comparison is not None else {}
    for offset, row in enumerate(balance.rows, start=1):
        target = header_row + offset
        sheet.cell(row=target, column=1, value=row.code)
        sheet.cell(row=target, column=2, value=row.name_ar)
        sheet.cell(row=target, column=3, value=row.debit_minor / 100)
        sheet.cell(row=target, column=4, value=row.credit_minor / 100)
        sheet.cell(row=target, column=5, value=row.balance_minor / 100)
        if comparing:
            previous = prior.get(row.account_id)
            sheet.cell(
                row=target,
                column=6,
                value=(previous.debit_minor if previous is not None else 0) / 100,
            )
            sheet.cell(
                row=target,
                column=7,
                value=(previous.credit_minor if previous is not None else 0) / 100,
            )
            sheet.cell(
                row=target,
                column=8,
                value=(previous.balance_minor if previous is not None else 0) / 100,
            )
    total_row = header_row + len(balance.rows) + 1
    sheet.cell(row=total_row, column=2, value="الإجمالي").font = header_font
    sheet.cell(row=total_row, column=3, value=balance.total_debit_minor / 100).font = header_font
    sheet.cell(row=total_row, column=4, value=balance.total_credit_minor / 100).font = header_font
    sheet.cell(
        row=total_row,
        column=5,
        value=(balance.total_debit_minor - balance.total_credit_minor) / 100,
    ).font = header_font
    if comparing and comparison is not None:
        sheet.cell(
            row=total_row, column=6, value=comparison.total_debit_minor / 100
        ).font = header_font
        sheet.cell(
            row=total_row, column=7, value=comparison.total_credit_minor / 100
        ).font = header_font
        sheet.cell(
            row=total_row,
            column=8,
            value=(comparison.total_debit_minor - comparison.total_credit_minor) / 100,
        ).font = header_font
    for column, width in enumerate(
        [14, 40, 16, 16, 16, 16, 16, 16] if comparing else [14, 40, 16, 16, 16], start=1
    ):
        sheet.column_dimensions[chr(ord("A") + column - 1)].width = width
    for row in sheet.iter_rows(min_row=total_row, max_row=total_row):
        for cell in row:
            cell.alignment = Alignment(horizontal="center")
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def iter_report_rows(balance: TrialBalance) -> Iterable[tuple[str, str, int, int, int]]:
    """Yield (code, name, debit, credit, balance) tuples for callers that need a flat view."""
    for row in balance.rows:
        yield row.code, row.name_ar, row.debit_minor, row.credit_minor, row.balance_minor
    yield (
        "",
        "الإجمالي",
        balance.total_debit_minor,
        balance.total_credit_minor,
        (balance.total_debit_minor - balance.total_credit_minor),
    )


def _table_pdf(
    *,
    title_ar: str,
    company_name_ar: str,
    subtitle_lines: list[str],
    headers_ar: list[str],
    rows: list[list[str]],
    widths: list[float],
    total_row: list[str] | None = None,
    meta_lines: list[str] | None = None,
) -> bytes:
    """Render one generic Arabic report table as an A4 landscape PDF."""
    font = _font()
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=landscape(A4),
        leftMargin=15 * mm,
        rightMargin=15 * mm,
        topMargin=15 * mm,
        bottomMargin=15 * mm,
        title=title_ar,
    )
    title = ParagraphStyle(name="MahmoudTitle", fontName=font, fontSize=16, leading=20, alignment=1)
    subtitle = ParagraphStyle(
        name="MahmoudSubtitle", fontName=font, fontSize=10, leading=14, alignment=1
    )
    story: list[Flowable] = [
        Paragraph(arabic(title_ar), title),
        Paragraph(arabic(company_name_ar), subtitle),
    ]
    story.extend(Paragraph(line, subtitle) for line in subtitle_lines)
    for line in meta_lines or []:
        story.append(Paragraph(arabic(line), subtitle))
    story.append(Spacer(1, 6 * mm))
    data: list[list[object]] = [[_p(arabic(header), font, bold=True) for header in headers_ar]]
    for row in rows:
        data.append([_p(arabic(value) if _has_arabic(value) else value, font) for value in row])
    if total_row is not None:
        data.append([_p(arabic(value), font, bold=True) for value in total_row])
    table = Table(data, colWidths=widths, repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8e8e8")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    story.append(table)
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(arabic("أنشئ بواسطة المحمود"), subtitle))
    document.build(story)
    return buffer.getvalue()


def _has_arabic(value: str) -> bool:
    return any("\u0600" <= character <= "\u06ff" for character in value)


def _table_xlsx(
    *,
    sheet_title: str,
    company_name_ar: str,
    subtitle_lines: list[str],
    headers_ar: list[str],
    rows: list[list[str | int | float]],
    widths: list[float],
    numeric_columns: set[int] | None = None,
    meta_lines: list[str] | None = None,
) -> bytes:
    """Render one generic report table as a right-to-left Excel workbook."""
    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = sheet_title
    sheet.sheet_view.rightToLeft = True
    header_font = Font(bold=True, size=12)
    column_count = max(1, len(headers_ar))
    sheet.append([company_name_ar])
    sheet.merge_cells(start_row=1, start_column=1, end_row=1, end_column=column_count)
    sheet.cell(row=1, column=1).font = Font(bold=True, size=14)
    sheet.cell(row=1, column=1).alignment = Alignment(horizontal="center")
    for line in subtitle_lines:
        sheet.append([line])
        sheet.merge_cells(
            start_row=sheet.max_row, start_column=1, end_row=sheet.max_row, end_column=column_count
        )
    for line in meta_lines or []:
        sheet.append([line])
        sheet.merge_cells(
            start_row=sheet.max_row, start_column=1, end_row=sheet.max_row, end_column=column_count
        )
    sheet.append([])
    header_row = sheet.max_row + 1
    for column, header in enumerate(headers_ar, start=1):
        cell = sheet.cell(row=header_row, column=column, value=header)
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center")
    for offset, row in enumerate(rows, start=1):
        target = header_row + offset
        for column, value in enumerate(row, start=1):
            cell = sheet.cell(row=target, column=column, value=value)
            if numeric_columns and column in numeric_columns and isinstance(value, (int, float)):
                cell.number_format = "#,##0.00"
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[chr(ord("A") + index - 1)].width = width
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _minor_or_blank(minor: int) -> str:
    return _amount(minor) if minor else ""


def render_profit_and_loss_pdf(
    report: ProfitAndLoss,
    *,
    company_name_ar: str,
    company_name_en: str | None = None,
    comparison: ProfitAndLoss | None = None,
    meta_lines: list[str] | None = None,
) -> bytes:
    """Render the profit and loss statement as an Arabic PDF."""
    comparing = comparison is not None
    prior: dict[UUID, int] = {}
    if comparison is not None:
        prior = {
            row.account_id: row.amount_minor for row in comparison.revenue + comparison.expenses
        }

    def section(title: str) -> list[str]:
        row = ["", title, ""]
        if comparing:
            row.append("")
        return row

    def line(code: str, name: str, amount: int, previous: int) -> list[str]:
        row = [code, name, _amount(amount)]
        if comparing:
            row.append(_amount(previous))
        return row

    def total(title: str, amount: int, previous: int) -> list[str]:
        row = ["", title, _amount(amount)]
        if comparing:
            row.append(_amount(previous))
        return row

    rows: list[list[str]] = [section("الإيرادات")]
    for row in report.revenue:
        rows.append(line(row.code, row.name_ar, row.amount_minor, prior.get(row.account_id, 0)))
    rows.append(
        total(
            "إجمالي الإيرادات",
            report.total_revenue_minor,
            comparison.total_revenue_minor if comparison is not None else 0,
        )
    )
    rows.append(section("المصروفات"))
    for row in report.expenses:
        rows.append(line(row.code, row.name_ar, row.amount_minor, prior.get(row.account_id, 0)))
    rows.append(
        total(
            "إجمالي المصروفات",
            report.total_expense_minor,
            comparison.total_expense_minor if comparison is not None else 0,
        )
    )
    subtitle_lines = [
        f"{report.start_date.isoformat()} - {report.end_date.isoformat()}",
        company_name_en or "",
    ]
    if comparison is not None:
        comparison_period = (
            f"فترة المقارنة: {comparison.start_date.isoformat()} - "
            f"{comparison.end_date.isoformat()}"
        )
        subtitle_lines.append(comparison_period)
    return _table_pdf(
        meta_lines=meta_lines,
        title_ar="قائمة الأرباح والخسائر",
        company_name_ar=company_name_ar,
        subtitle_lines=subtitle_lines,
        headers_ar=["رمز الحساب", "اسم الحساب", "المبلغ"]
        + (["المبلغ السابق"] if comparing else []),
        rows=rows,
        widths=[35 * mm, 130 * mm, 60 * mm]
        if not comparing
        else [30 * mm, 100 * mm, 55 * mm, 55 * mm],
        total_row=total(
            "صافي الربح",
            report.net_profit_minor,
            comparison.net_profit_minor if comparison is not None else 0,
        ),
    )


def render_profit_and_loss_xlsx(
    report: ProfitAndLoss,
    *,
    company_name_ar: str,
    company_name_en: str | None = None,
    comparison: ProfitAndLoss | None = None,
    meta_lines: list[str] | None = None,
) -> bytes:
    """Render the profit and loss statement as a right-to-left workbook."""
    comparing = comparison is not None
    prior: dict[UUID, int] = {}
    if comparison is not None:
        prior = {
            row.account_id: row.amount_minor for row in comparison.revenue + comparison.expenses
        }

    def section(title: str) -> list[str | int | float]:
        row: list[str | int | float] = ["", title, ""]
        if comparing:
            row.append("")
        return row

    def line(code: str, name: str, amount: int, previous: int) -> list[str | int | float]:
        row: list[str | int | float] = [code, name, amount / 100]
        if comparing:
            row.append(previous / 100)
        return row

    def total(title: str, amount: int, previous: int) -> list[str | int | float]:
        row: list[str | int | float] = ["", title, amount / 100]
        if comparing:
            row.append(previous / 100)
        return row

    rows: list[list[str | int | float]] = [section("الإيرادات")]
    for row in report.revenue:
        rows.append(line(row.code, row.name_ar, row.amount_minor, prior.get(row.account_id, 0)))
    rows.append(
        total(
            "إجمالي الإيرادات",
            report.total_revenue_minor,
            comparison.total_revenue_minor if comparison is not None else 0,
        )
    )
    rows.append(section("المصروفات"))
    for row in report.expenses:
        rows.append(line(row.code, row.name_ar, row.amount_minor, prior.get(row.account_id, 0)))
    rows.append(
        total(
            "إجمالي المصروفات",
            report.total_expense_minor,
            comparison.total_expense_minor if comparison is not None else 0,
        )
    )
    rows.append(
        total(
            "صافي الربح",
            report.net_profit_minor,
            comparison.net_profit_minor if comparison is not None else 0,
        )
    )
    subtitle_lines = [
        f"{report.start_date.isoformat()} - {report.end_date.isoformat()}",
        company_name_en or "",
    ]
    if comparison is not None:
        comparison_period = (
            f"فترة المقارنة: {comparison.start_date.isoformat()} - "
            f"{comparison.end_date.isoformat()}"
        )
        subtitle_lines.append(comparison_period)
    return _table_xlsx(
        meta_lines=meta_lines,
        sheet_title="الأرباح والخسائر",
        company_name_ar=company_name_ar,
        subtitle_lines=subtitle_lines,
        headers_ar=["رمز الحساب", "اسم الحساب", "المبلغ"]
        + (["المبلغ السابق"] if comparing else []),
        rows=rows,
        widths=[16, 50, 20] if not comparing else [16, 50, 20, 20],
        numeric_columns={3, 4} if comparing else {3},
    )


def render_balance_sheet_pdf(
    report: BalanceSheet,
    *,
    company_name_ar: str,
    company_name_en: str | None = None,
    comparison: BalanceSheet | None = None,
    meta_lines: list[str] | None = None,
) -> bytes:
    """Render the balance sheet as an Arabic PDF."""
    comparing = comparison is not None
    prior: dict[UUID, int] = {}
    if comparison is not None:
        prior = {
            row.account_id: row.amount_minor
            for row in comparison.assets + comparison.liabilities + comparison.equity
        }

    def section(title: str) -> list[str]:
        row = ["", title, ""]
        if comparing:
            row.append("")
        return row

    def line(code: str, name: str, amount: int, previous: int) -> list[str]:
        row = [code, name, _amount(amount)]
        if comparing:
            row.append(_amount(previous))
        return row

    def total(title: str, amount: int, previous: int) -> list[str]:
        row = ["", title, _amount(amount)]
        if comparing:
            row.append(_amount(previous))
        return row

    rows: list[list[str]] = [section("الأصول")]
    for row in report.assets:
        rows.append(line(row.code, row.name_ar, row.amount_minor, prior.get(row.account_id, 0)))
    rows.append(
        total(
            "إجمالي الأصول",
            report.total_assets_minor,
            comparison.total_assets_minor if comparison is not None else 0,
        )
    )
    rows.append(section("الالتزامات"))
    for row in report.liabilities:
        rows.append(line(row.code, row.name_ar, row.amount_minor, prior.get(row.account_id, 0)))
    rows.append(
        total(
            "إجمالي الالتزامات",
            report.total_liabilities_minor,
            comparison.total_liabilities_minor if comparison is not None else 0,
        )
    )
    rows.append(section("حقوق الملكية"))
    for row in report.equity:
        rows.append(line(row.code, row.name_ar, row.amount_minor, prior.get(row.account_id, 0)))
    rows.append(
        line(
            "",
            "أرباح الفترة الحالية",
            report.current_earnings_minor,
            comparison.current_earnings_minor if comparison is not None else 0,
        )
    )
    subtitle_lines = [report.as_of.isoformat(), company_name_en or ""]
    if comparison is not None:
        subtitle_lines.append(f"فترة المقارنة: {comparison.as_of.isoformat()}")
    return _table_pdf(
        meta_lines=meta_lines,
        title_ar="الميزانية العمومية",
        company_name_ar=company_name_ar,
        subtitle_lines=subtitle_lines,
        headers_ar=["رمز الحساب", "اسم الحساب", "المبلغ"]
        + (["المبلغ السابق"] if comparing else []),
        rows=rows,
        widths=[35 * mm, 130 * mm, 60 * mm]
        if not comparing
        else [30 * mm, 100 * mm, 55 * mm, 55 * mm],
        total_row=total(
            "إجمالي حقوق الملكية",
            report.total_equity_minor,
            comparison.total_equity_minor if comparison is not None else 0,
        ),
    )


def render_balance_sheet_xlsx(
    report: BalanceSheet,
    *,
    company_name_ar: str,
    company_name_en: str | None = None,
    comparison: BalanceSheet | None = None,
    meta_lines: list[str] | None = None,
) -> bytes:
    """Render the balance sheet as a right-to-left workbook."""
    comparing = comparison is not None
    prior: dict[UUID, int] = {}
    if comparison is not None:
        prior = {
            row.account_id: row.amount_minor
            for row in comparison.assets + comparison.liabilities + comparison.equity
        }

    def section(title: str) -> list[str | int | float]:
        row: list[str | int | float] = ["", title, ""]
        if comparing:
            row.append("")
        return row

    def line(code: str, name: str, amount: int, previous: int) -> list[str | int | float]:
        row: list[str | int | float] = [code, name, amount / 100]
        if comparing:
            row.append(previous / 100)
        return row

    def total(title: str, amount: int, previous: int) -> list[str | int | float]:
        row: list[str | int | float] = ["", title, amount / 100]
        if comparing:
            row.append(previous / 100)
        return row

    rows: list[list[str | int | float]] = [section("الأصول")]
    for row in report.assets:
        rows.append(line(row.code, row.name_ar, row.amount_minor, prior.get(row.account_id, 0)))
    rows.append(
        total(
            "إجمالي الأصول",
            report.total_assets_minor,
            comparison.total_assets_minor if comparison is not None else 0,
        )
    )
    rows.append(section("الالتزامات"))
    for row in report.liabilities:
        rows.append(line(row.code, row.name_ar, row.amount_minor, prior.get(row.account_id, 0)))
    rows.append(
        total(
            "إجمالي الالتزامات",
            report.total_liabilities_minor,
            comparison.total_liabilities_minor if comparison is not None else 0,
        )
    )
    rows.append(section("حقوق الملكية"))
    for row in report.equity:
        rows.append(line(row.code, row.name_ar, row.amount_minor, prior.get(row.account_id, 0)))
    rows.append(
        line(
            "",
            "أرباح الفترة الحالية",
            report.current_earnings_minor,
            comparison.current_earnings_minor if comparison is not None else 0,
        )
    )
    rows.append(
        total(
            "إجمالي حقوق الملكية",
            report.total_equity_minor,
            comparison.total_equity_minor if comparison is not None else 0,
        )
    )
    subtitle_lines = [report.as_of.isoformat(), company_name_en or ""]
    if comparison is not None:
        subtitle_lines.append(f"فترة المقارنة: {comparison.as_of.isoformat()}")
    return _table_xlsx(
        meta_lines=meta_lines,
        sheet_title="الميزانية العمومية",
        company_name_ar=company_name_ar,
        subtitle_lines=subtitle_lines,
        headers_ar=["رمز الحساب", "اسم الحساب", "المبلغ"]
        + (["المبلغ السابق"] if comparing else []),
        rows=rows,
        widths=[16, 50, 20] if not comparing else [16, 50, 20, 20],
        numeric_columns={3, 4} if comparing else {3},
    )


def _statement_movement(row: StatementRow) -> list[str]:
    """One movement row shared by the current and prior statement sections."""
    return [
        row.entry_date.isoformat(),
        row.number,
        row.description or row.document_type,
        _minor_or_blank(row.debit_minor),
        _minor_or_blank(row.credit_minor),
        _amount(row.balance_minor),
    ]


def _statement_section(statement: PartyStatement, heading: str) -> list[list[str]]:
    """The heading, opening balance and movements of one statement section."""
    rows: list[list[str]] = [
        [
            statement.start_date.isoformat(),
            "",
            f"{heading}: {statement.start_date.isoformat()} - {statement.end_date.isoformat()}",
            "",
            "",
            "",
        ],
        ["", "", "الرصيد الافتتاحي", "", "", _amount(statement.opening_balance_minor)],
    ]
    for row in statement.rows:
        rows.append(_statement_movement(row))
    return rows


def render_statement_pdf(
    statement: PartyStatement,
    *,
    company_name_ar: str,
    title_ar: str,
    company_name_en: str | None = None,
    meta_lines: list[str] | None = None,
    comparison: PartyStatement | None = None,
) -> bytes:
    """Render a customer or supplier statement as an Arabic PDF.

    With a comparison statement the prior period is printed above the requested
    period so both closing balances appear in one document.
    """
    rows: list[list[str]] = []
    if comparison is not None:
        rows.extend(_statement_section(comparison, "الفترة السابقة"))
        rows.append(
            [
                "",
                "",
                f"الرصيد الختامي السابق: {_amount(comparison.closing_balance_minor)}",
                _amount(comparison.total_debit_minor),
                _amount(comparison.total_credit_minor),
                "",
            ]
        )
        rows.extend(_statement_section(statement, "الفترة الحالية"))
    else:
        rows = _statement_section(statement, "الفترة")
    return _table_pdf(
        meta_lines=meta_lines,
        title_ar=title_ar,
        company_name_ar=company_name_ar,
        subtitle_lines=[
            statement.party_name_ar,
            f"{statement.start_date.isoformat()} - {statement.end_date.isoformat()}",
            company_name_en or "",
        ],
        headers_ar=["التاريخ", "المستند", "البيان", "مدين", "دائن", "الرصيد"],
        rows=rows,
        widths=[28 * mm, 35 * mm, 75 * mm, 32 * mm, 32 * mm, 35 * mm],
        total_row=[
            "",
            "",
            "الرصيد الختامي",
            _amount(statement.total_debit_minor),
            _amount(statement.total_credit_minor),
            _amount(statement.closing_balance_minor),
        ],
    )


def _statement_movement_xlsx(row: StatementRow) -> list[str | int | float]:
    """One movement row shared by the current and prior statement sections."""
    return [
        row.entry_date.isoformat(),
        row.number,
        row.description or row.document_type,
        row.debit_minor / 100,
        row.credit_minor / 100,
        row.balance_minor / 100,
    ]


def _statement_section_xlsx(
    statement: PartyStatement, heading: str
) -> list[list[str | int | float]]:
    """The heading, opening balance and movements of one statement section."""
    rows: list[list[str | int | float]] = [
        [
            statement.start_date.isoformat(),
            "",
            f"{heading}: {statement.start_date.isoformat()} - {statement.end_date.isoformat()}",
            "",
            "",
            "",
        ],
        ["", "", "الرصيد الافتتاحي", "", "", statement.opening_balance_minor / 100],
    ]
    for row in statement.rows:
        rows.append(_statement_movement_xlsx(row))
    return rows


def render_statement_xlsx(
    statement: PartyStatement,
    *,
    company_name_ar: str,
    title_ar: str,
    company_name_en: str | None = None,
    meta_lines: list[str] | None = None,
    comparison: PartyStatement | None = None,
) -> bytes:
    """Render a customer or supplier statement as a right-to-left workbook.

    With a comparison statement the prior period is printed above the requested
    period so both closing balances appear in one workbook.
    """
    rows: list[list[str | int | float]] = []
    if comparison is not None:
        rows.extend(_statement_section_xlsx(comparison, "الفترة السابقة"))
        rows.append(
            [
                "",
                "",
                f"الرصيد الختامي السابق: {_amount(comparison.closing_balance_minor)}",
                comparison.total_debit_minor / 100,
                comparison.total_credit_minor / 100,
                "",
            ]
        )
        rows.extend(_statement_section_xlsx(statement, "الفترة الحالية"))
    else:
        rows = _statement_section_xlsx(statement, "الفترة")
    rows.append(
        [
            "",
            "",
            "الرصيد الختامي",
            statement.total_debit_minor / 100,
            statement.total_credit_minor / 100,
            statement.closing_balance_minor / 100,
        ]
    )
    return _table_xlsx(
        meta_lines=meta_lines,
        sheet_title=title_ar,
        company_name_ar=company_name_ar,
        subtitle_lines=[
            statement.party_name_ar,
            f"{statement.start_date.isoformat()} - {statement.end_date.isoformat()}",
            company_name_en or "",
        ],
        headers_ar=["التاريخ", "المستند", "البيان", "مدين", "دائن", "الرصيد"],
        rows=rows,
        widths=[14, 18, 40, 16, 16, 16],
        numeric_columns={4, 5, 6},
    )


def render_aging_pdf(
    report: AgingReport,
    *,
    company_name_ar: str,
    title_ar: str,
    company_name_en: str | None = None,
    meta_lines: list[str] | None = None,
) -> bytes:
    """Render an AR or AP aging report as an Arabic PDF."""
    rows = [
        [
            row.party_name_ar,
            row.document_number,
            row.document_date.isoformat(),
            row.due_date.isoformat(),
            str(row.days_overdue),
            _amount(row.base_outstanding_minor),
        ]
        for row in report.rows
    ]
    return _table_pdf(
        meta_lines=meta_lines,
        title_ar=title_ar,
        company_name_ar=company_name_ar,
        subtitle_lines=[report.as_of.isoformat(), company_name_en or ""],
        headers_ar=["الطرف", "المستند", "التاريخ", "الاستحقاق", "أيام التأخير", "المتبقي"],
        rows=rows,
        widths=[60 * mm, 35 * mm, 28 * mm, 28 * mm, 25 * mm, 35 * mm],
        total_row=[
            "",
            "",
            "",
            "",
            "",
            _amount(report.total_base_outstanding_minor),
        ],
    )


def render_aging_xlsx(
    report: AgingReport,
    *,
    company_name_ar: str,
    title_ar: str,
    company_name_en: str | None = None,
    meta_lines: list[str] | None = None,
) -> bytes:
    """Render an AR or AP aging report as a right-to-left workbook."""
    rows: list[list[str | int | float]] = [
        [
            row.party_name_ar,
            row.document_number,
            row.document_date.isoformat(),
            row.due_date.isoformat(),
            row.days_overdue,
            row.base_outstanding_minor / 100,
        ]
        for row in report.rows
    ]
    rows.append(["", "", "", "", "", report.total_base_outstanding_minor / 100])
    return _table_xlsx(
        meta_lines=meta_lines,
        sheet_title=title_ar,
        company_name_ar=company_name_ar,
        subtitle_lines=[report.as_of.isoformat(), company_name_en or ""],
        headers_ar=["الطرف", "المستند", "التاريخ", "الاستحقاق", "أيام التأخير", "المتبقي"],
        rows=rows,
        widths=[32, 18, 14, 14, 12, 18],
        numeric_columns={6},
    )


def render_general_ledger_pdf(
    report: GeneralLedger,
    *,
    company_name_ar: str,
    company_name_en: str | None = None,
    meta_lines: list[str] | None = None,
) -> bytes:
    """Render one account ledger as an Arabic PDF."""
    rows: list[list[str]] = [
        [
            report.start_date.isoformat(),
            "",
            "الرصيد الافتتاحي",
            "",
            "",
            _amount(report.opening_balance_minor),
        ]
    ]
    for row in report.rows:
        rows.append(
            [
                row.entry_date.isoformat(),
                row.entry_number,
                row.description or "",
                _minor_or_blank(row.debit_minor),
                _minor_or_blank(row.credit_minor),
                _amount(row.balance_minor),
            ]
        )
    return _table_pdf(
        meta_lines=meta_lines,
        title_ar="دفتر الأستاذ",
        company_name_ar=company_name_ar,
        subtitle_lines=[
            f"{report.account_code} - {report.account_name_ar}",
            f"{report.start_date.isoformat()} - {report.end_date.isoformat()}",
            company_name_en or "",
        ],
        headers_ar=["التاريخ", "القيد", "البيان", "مدين", "دائن", "الرصيد"],
        rows=rows,
        widths=[28 * mm, 35 * mm, 80 * mm, 30 * mm, 30 * mm, 32 * mm],
        total_row=[
            "",
            "",
            "الرصيد الختامي",
            _amount(sum(row.debit_minor for row in report.rows)),
            _amount(sum(row.credit_minor for row in report.rows)),
            _amount(report.closing_balance_minor),
        ],
    )


def render_general_ledger_xlsx(
    report: GeneralLedger,
    *,
    company_name_ar: str,
    company_name_en: str | None = None,
    meta_lines: list[str] | None = None,
) -> bytes:
    """Render one account ledger as a right-to-left workbook."""
    rows: list[list[str | int | float]] = [
        [
            report.start_date.isoformat(),
            "",
            "الرصيد الافتتاحي",
            "",
            "",
            report.opening_balance_minor / 100,
        ]
    ]
    for row in report.rows:
        rows.append(
            [
                row.entry_date.isoformat(),
                row.entry_number,
                row.description or "",
                row.debit_minor / 100,
                row.credit_minor / 100,
                row.balance_minor / 100,
            ]
        )
    rows.append(
        [
            "",
            "",
            "الرصيد الختامي",
            sum(row.debit_minor for row in report.rows) / 100,
            sum(row.credit_minor for row in report.rows) / 100,
            report.closing_balance_minor / 100,
        ]
    )
    return _table_xlsx(
        meta_lines=meta_lines,
        sheet_title="دفتر الأستاذ",
        company_name_ar=company_name_ar,
        subtitle_lines=[
            f"{report.account_code} - {report.account_name_ar}",
            f"{report.start_date.isoformat()} - {report.end_date.isoformat()}",
            company_name_en or "",
        ],
        headers_ar=["التاريخ", "القيد", "البيان", "مدين", "دائن", "الرصيد"],
        rows=rows,
        widths=[14, 18, 40, 16, 16, 16],
        numeric_columns={4, 5, 6},
    )


REGISTER_HEADERS_AR = ("النوع", "الرقم", "التاريخ", "الحالة", "العملة", "المبلغ", "المبلغ الأساسي")


def _register_values(report: DocumentRegister) -> list[list[str]]:
    return [
        [
            row.document_type,
            row.number,
            row.document_date.isoformat(),
            row.status,
            row.currency,
            _amount(row.amount_minor),
            _amount(row.base_amount_minor),
        ]
        for row in report.rows
    ]


def render_document_register_pdf(
    report: DocumentRegister,
    *,
    company_name_ar: str,
    company_name_en: str | None = None,
    meta_lines: list[str] | None = None,
) -> bytes:
    """Render the document register as an Arabic PDF."""
    return _table_pdf(
        meta_lines=meta_lines,
        title_ar="سجل المستندات",
        company_name_ar=company_name_ar,
        subtitle_lines=[
            f"{report.start_date.isoformat()} - {report.end_date.isoformat()}",
            f"عدد المستندات: {len(report.rows)}",
        ],
        headers_ar=list(REGISTER_HEADERS_AR),
        rows=_register_values(report),
        widths=[32 * mm, 32 * mm, 24 * mm, 20 * mm, 16 * mm, 26 * mm, 30 * mm],
    )


def render_document_register_xlsx(
    report: DocumentRegister,
    *,
    company_name_ar: str,
    company_name_en: str | None = None,
    meta_lines: list[str] | None = None,
) -> bytes:
    """Render the document register as a right-to-left Excel workbook."""
    return _table_xlsx(
        meta_lines=meta_lines,
        sheet_title="Document register",
        company_name_ar=company_name_ar,
        subtitle_lines=[
            f"{report.start_date.isoformat()} - {report.end_date.isoformat()}",
            f"Documents: {len(report.rows)}",
        ],
        headers_ar=list(REGISTER_HEADERS_AR),
        rows=cast("list[list[str | int | float]]", _register_values(report)),
        widths=[24, 24, 14, 12, 10, 16, 18],
        numeric_columns={6, 7},
    )


INVENTORY_HEADERS_AR = ("الكود", "الصنف", "وحدة القياس", "الكمية", "القيمة")


def _inventory_values(report: InventoryValuation) -> list[list[str]]:
    return [
        [
            row.code,
            row.name_ar,
            row.unit_of_measure,
            f"{row.quantity_on_hand:.4f}",
            _amount(row.stock_value_minor),
        ]
        for row in report.rows
    ]


def render_inventory_valuation_pdf(
    report: InventoryValuation,
    *,
    company_name_ar: str,
    company_name_en: str | None = None,
    meta_lines: list[str] | None = None,
) -> bytes:
    """Render the inventory valuation as an Arabic PDF."""
    return _table_pdf(
        meta_lines=meta_lines,
        title_ar="تقييم المخزون",
        company_name_ar=company_name_ar,
        subtitle_lines=[
            f"عدد الأصناف: {len(report.rows)}",
            f"إجمالي القيمة: {_amount(report.total_value_minor)}",
        ],
        headers_ar=list(INVENTORY_HEADERS_AR),
        rows=_inventory_values(report),
        widths=[30 * mm, 70 * mm, 28 * mm, 28 * mm, 32 * mm],
        total_row=["الإجمالي", "", "", "", _amount(report.total_value_minor)],
    )


def render_inventory_valuation_xlsx(
    report: InventoryValuation,
    *,
    company_name_ar: str,
    company_name_en: str | None = None,
    meta_lines: list[str] | None = None,
) -> bytes:
    """Render the inventory valuation as a right-to-left Excel workbook."""
    rows: list[list[str | int | float]] = [
        [
            row.code,
            row.name_ar,
            row.unit_of_measure,
            float(row.quantity_on_hand),
            row.stock_value_minor / 100,
        ]
        for row in report.rows
    ]
    return _table_xlsx(
        meta_lines=meta_lines,
        sheet_title="تقييم المخزون",
        company_name_ar=company_name_ar,
        subtitle_lines=[
            f"عدد الأصناف: {len(report.rows)}",
            f"إجمالي القيمة: {_amount(report.total_value_minor)}",
            company_name_en or "",
        ],
        headers_ar=list(INVENTORY_HEADERS_AR),
        rows=rows,
        widths=[16, 36, 14, 14, 16],
        numeric_columns={4, 5},
    )


STOCK_MOVEMENT_HEADERS_AR = (
    "التاريخ",
    "النوع",
    "المستودع",
    "الصنف",
    "الكمية",
    "تكلفة الوحدة",
    "القيمة",
    "نوع المستند",
)


def _stock_movement_values(report: StockMovementReport) -> list[list[str]]:
    return [
        [
            row.occurred_on.isoformat(),
            row.move_type,
            row.warehouse_code,
            f"{row.item_code} · {row.item_name_ar}",
            f"{row.quantity:.4f}",
            _amount(row.unit_cost_minor),
            _amount(row.value_minor),
            row.document_type,
        ]
        for row in report.rows
    ]


def render_stock_movement_pdf(
    report: StockMovementReport,
    *,
    company_name_ar: str,
    company_name_en: str | None = None,
    meta_lines: list[str] | None = None,
) -> bytes:
    """Render the stock movement report as an Arabic PDF."""
    return _table_pdf(
        meta_lines=meta_lines,
        title_ar="حركة المخزون",
        company_name_ar=company_name_ar,
        subtitle_lines=[
            f"{report.start_date.isoformat()} - {report.end_date.isoformat()}",
            f"عدد الحركات: {len(report.rows)}",
            f"إجمالي الاستلام: {_amount(report.total_receipt_value_minor)} · "
            f"إجمالي الصرف: {_amount(report.total_issue_value_minor)}",
        ],
        headers_ar=list(STOCK_MOVEMENT_HEADERS_AR),
        rows=_stock_movement_values(report),
        widths=[24 * mm, 18 * mm, 24 * mm, 50 * mm, 22 * mm, 26 * mm, 26 * mm, 28 * mm],
    )


def render_stock_movement_xlsx(
    report: StockMovementReport,
    *,
    company_name_ar: str,
    company_name_en: str | None = None,
    meta_lines: list[str] | None = None,
) -> bytes:
    """Render the stock movement report as a right-to-left Excel workbook."""
    rows: list[list[str | int | float]] = [
        [
            row.occurred_on.isoformat(),
            row.move_type,
            row.warehouse_code,
            f"{row.item_code} · {row.item_name_ar}",
            float(row.quantity),
            row.unit_cost_minor / 100,
            row.value_minor / 100,
            row.document_type,
        ]
        for row in report.rows
    ]
    return _table_xlsx(
        meta_lines=meta_lines,
        sheet_title="حركة المخزون",
        company_name_ar=company_name_ar,
        subtitle_lines=[
            f"{report.start_date.isoformat()} - {report.end_date.isoformat()}",
            f"عدد الحركات: {len(report.rows)}",
            company_name_en or "",
        ],
        headers_ar=list(STOCK_MOVEMENT_HEADERS_AR),
        rows=rows,
        widths=[14, 12, 16, 34, 12, 16, 16, 18],
        numeric_columns={5, 6, 7},
    )


VAT_HEADERS_AR = ("البند", "المبلغ")


def _vat_values(report: TaxReturn) -> list[list[str]]:
    return [
        ["ضريبة المخرجات", _amount(report.output_vat_minor)],
        ["إشعارات دائنة (مخرجات)", _amount(report.output_credit_notes_minor)],
        ["صافي ضريبة المخرجات", _amount(report.net_output_vat_minor)],
        ["ضريبة المدخلات", _amount(report.input_vat_minor)],
        ["إشعارات مدينة (مدخلات)", _amount(report.input_debit_notes_minor)],
        ["صافي ضريبة المدخلات", _amount(report.net_input_vat_minor)],
        ["المستحق للفترة", _amount(report.payable_minor)],
        ["عدد فواتير المبيعات", str(report.invoice_count)],
        ["عدد فواتير الشراء", str(report.bill_count)],
    ]


def render_tax_return_pdf(
    report: TaxReturn,
    *,
    company_name_ar: str,
    company_name_en: str | None = None,
    meta_lines: list[str] | None = None,
) -> bytes:
    """Render the VAT return summary as an Arabic PDF."""
    return _table_pdf(
        meta_lines=meta_lines,
        title_ar="الإقرار الضريبي",
        company_name_ar=company_name_ar,
        subtitle_lines=[f"{report.start_date.isoformat()} - {report.end_date.isoformat()}"],
        headers_ar=list(VAT_HEADERS_AR),
        rows=_vat_values(report),
        widths=[110 * mm, 50 * mm],
    )


def render_tax_return_xlsx(
    report: TaxReturn,
    *,
    company_name_ar: str,
    company_name_en: str | None = None,
    meta_lines: list[str] | None = None,
) -> bytes:
    """Render the VAT return summary as a right-to-left Excel workbook."""
    rows: list[list[str | int | float]] = [[label, amount] for label, amount in _vat_values(report)]
    return _table_xlsx(
        meta_lines=meta_lines,
        sheet_title="الإقرار الضريبي",
        company_name_ar=company_name_ar,
        subtitle_lines=[
            f"{report.start_date.isoformat()} - {report.end_date.isoformat()}",
            company_name_en or "",
        ],
        headers_ar=list(VAT_HEADERS_AR),
        rows=rows,
        widths=[36, 20],
    )


INVOICE_HEADERS_AR = ("الوصف", "الكمية", "سعر الوحدة", "الإجمالي")


def render_document_pdf(
    *,
    title_ar: str,
    company_name_ar: str,
    number: str,
    document_date: str,
    party_label_ar: str,
    party_name: str,
    currency: str,
    status: str,
    lines: list[tuple[str, str, int, int]],
    subtotal_minor: int,
    tax_minor: int,
    total_minor: int,
) -> bytes:
    """Render a sales invoice or purchase bill as an Arabic A4 portrait PDF."""
    rows: list[list[str]] = [
        [description, quantity, _amount(unit_price), _amount(line_total)]
        for description, quantity, unit_price, line_total in lines
    ]
    return _table_pdf(
        title_ar=title_ar,
        company_name_ar=company_name_ar,
        subtitle_lines=[
            f"رقم المستند: {number}",
            f"التاريخ: {document_date}",
            f"{party_label_ar}: {party_name}",
            f"العملة: {currency} · الحالة: {status}",
            f"المجموع قبل الضريبة: {_amount(subtotal_minor)} · الضريبة: {_amount(tax_minor)}",
        ],
        headers_ar=list(INVOICE_HEADERS_AR),
        rows=rows,
        widths=[110 * mm, 25 * mm, 30 * mm, 35 * mm],
        total_row=["الإجمالي", "", "", _amount(total_minor)],
    )


AUDIT_HEADERS_AR = (
    "الوقت",
    "الإجراء",
    "نوع الكيان",
    "معرف الكيان",
    "المستخدم",
    "معرف الطلب",
    "عنوان IP",
    "التفاصيل",
)


def _audit_values(trail: AuditTrail) -> list[list[str]]:
    values: list[list[str]] = []
    for row in trail.rows:
        detail = (
            json.dumps(row.detail, ensure_ascii=False, sort_keys=True)[:160]
            if row.detail is not None
            else ""
        )
        values.append(
            [
                row.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                row.action,
                row.entity_type or "",
                str(row.entity_id) if row.entity_id is not None else "",
                str(row.actor_user_id) if row.actor_user_id is not None else "",
                row.request_id or "",
                row.ip_address or "",
                detail,
            ]
        )
    return values


def render_audit_trail_pdf(
    trail: AuditTrail,
    *,
    company_name_ar: str,
    company_name_en: str | None = None,
    meta_lines: list[str] | None = None,
) -> bytes:
    """Render the audit trail as an Arabic PDF."""
    return _table_pdf(
        meta_lines=meta_lines,
        title_ar="سجل التدقيق",
        company_name_ar=company_name_ar,
        subtitle_lines=[f"{trail.start_date.isoformat()} - {trail.end_date.isoformat()}"],
        headers_ar=list(AUDIT_HEADERS_AR),
        rows=_audit_values(trail),
        widths=[30 * mm, 34 * mm, 26 * mm, 34 * mm, 34 * mm, 34 * mm, 26 * mm, 49 * mm],
    )


def render_audit_trail_xlsx(
    trail: AuditTrail,
    *,
    company_name_ar: str,
    company_name_en: str | None = None,
    meta_lines: list[str] | None = None,
) -> bytes:
    """Render the audit trail as a right-to-left Excel workbook."""
    return _table_xlsx(
        meta_lines=meta_lines,
        sheet_title="سجل التدقيق",
        company_name_ar=company_name_ar,
        subtitle_lines=[
            f"{trail.start_date.isoformat()} - {trail.end_date.isoformat()}",
            company_name_en or "",
        ],
        headers_ar=list(AUDIT_HEADERS_AR),
        rows=cast("list[list[str | int | float]]", _audit_values(trail)),
        widths=[20, 24, 18, 24, 24, 24, 18, 44],
    )
