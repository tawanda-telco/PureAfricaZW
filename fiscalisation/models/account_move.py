from datetime import datetime
import json

import requests
from odoo import models, fields, api, _
from odoo.exceptions import UserError
import base64
import logging

_logger = logging.getLogger(__name__)

class AccountMove(models.Model):
    _inherit = 'account.move'

    customer_vat = fields.Char(string='Customer VAT', compute='_compute_customer_vat', store=True)
    customer_tin = fields.Char(string='Customer TIN', compute='_compute_customer_tin', store=True)
    receipt_type = fields.Char(string='Receipt Type', compute='_compute_receipt_type')
    qr_url = fields.Char(string='QR Code URL', copy=False)
    qr_code = fields.Binary(string='QR Code', compute='_compute_qr_code', copy=False)
    fdms_url = fields.Char(string='FDMS URL', readonly=True, copy=False, compute='_compute_fdms_url')
    fiscal_date = fields.Datetime(string='Fiscalisation Date', copy=False)
    device_id = fields.Char(string='Device ID', readonly=True, compute='_compute_device_id', store=True)
    device_serial = fields.Char(string='Device Serial', readonly=True, compute='_compute_device_serial', store=True)
    receipt_global_number = fields.Char(string='Receipt Global Number', readonly=True, copy=False)
    receipt_number = fields.Char(string='Receipt Number', readonly=True, copy=False)
    fiscal_day_no = fields.Char(string='Fiscal Day', readonly=True, copy=False)
    verification_code = fields.Char(string='Verification Code', copy=False)
    fiscalised = fields.Boolean(string='Fiscalised', readonly=True, default=False, copy=False)

    @api.depends('partner_id')
    def _compute_customer_vat(self):
        for invoice in self:
            invoice.customer_vat = invoice.partner_id.vat or ''

    @api.depends('partner_id')
    def _compute_customer_tin(self):
        for invoice in self:
            invoice.customer_tin = invoice.partner_id.tin or ''

    @api.depends('move_type')
    def _compute_receipt_type(self):
        for invoice in self:
            if invoice.move_type == 'out_refund':
                invoice.receipt_type = 'CreditNote'
            elif invoice.move_type == 'in_refund':
                invoice.receipt_type = 'DebitNote'
            else:
                invoice.receipt_type = 'FiscalInvoice'

    @api.depends('company_id')
    def _compute_device_id(self):
        for invoice in self:
            device = self.env['fiscal.device'].search([
                ('company_id', '=', invoice.company_id.id)
            ], limit=1)
            invoice.device_id = device.device_id if device else False

    @api.depends('company_id')
    def _compute_device_serial(self):
        for invoice in self:
            device = self.env['fiscal.device'].search([
                ('company_id', '=', invoice.company_id.id)
            ], limit=1)
            invoice.device_serial = device.device_serial if device else False

    @api.depends('company_id')
    def _compute_fdms_url(self):
        for invoice in self:
            device = self.env['fiscal.device'].search([
                ('company_id', '=', invoice.company_id.id)
            ], limit=1)
            invoice.fdms_url = device.fdms_url if device else False

    def _compute_qr_code(self):
        for invoice in self:
            if invoice.verification_code:
                invoice.qr_code = self._generate_fiscal_invoice_qr_code(invoice.qr_url)
            else:
                invoice.qr_code = False

    def _generate_fiscal_invoice_qr_code(self, data):
        try:
            import qrcode
            from io import BytesIO
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=10,
                border=4,
            )
            qr.add_data(data)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buffer = BytesIO()
            img.save(buffer, format="PNG")
            return base64.b64encode(buffer.getvalue())
        except ImportError:
            _logger.warning("QR code generation requires python-qrcode library")
            return False

    def action_fiscalise_invoice(self):
        self.ensure_one()
        if self.fiscalised:
            raise UserError(_("This invoice is already fiscalised"))
        
        device = self.env['fiscal.device'].search([
            ('company_id', '=', self.company_id.id)
        ], limit=1)
        
        _logger.info("Initiating fiscalisation for %s (%s)", self.name, self.company_id.name)
        
        if not device:
            raise UserError(_("No fiscal device configured for this company"))

        try:
            payload = self._prepare_fiscal_payload()
            _logger.info("Fiscal payload prepared: %s", payload)
            # raise UserError(_("Fiscal payload prepared: %s", payload))
            
            response = device._api_request('/api/v1/receipts', payload=payload)
            _logger.info("API response received: %s", response)
            
            self._process_fiscal_response(response)
            return self._show_success_notification(response)
            
        except requests.exceptions.HTTPError as e:
            error_code = "UNKNOWN"
            error_message = str(e)
            
            # Try to extract error details from JSON response
            try:
                error_data = e.response.json()
                error_code = error_data.get('errorCode', error_code)
                error_message = error_data.get('detail', error_data.get('title', error_message))
            except json.JSONDecodeError:
                _logger.warning("Failed to parse error response: %s", e.response.text)
            
            # Handle specific error codes
            if error_code == 'RCPT013':
                error_message = _("This invoice number already exists in the fiscal system")
                # self.fiscalised = True  # Mark as fiscalised to prevent retries
                
            self.message_post(
                body=_("Fiscalisation Failed (Code: %s)<br>%s") % (error_code, error_message),
                subject=_("Fiscalisation Error"),
                message_type="comment"
            )
            
            raise UserError(_("Fiscalisation Error [%(code)s]: %(message)s") % {
                'code': error_code,
                'message': error_message
            })
            
        except Exception as e:
            self.message_post(
                body=_("Technical Error: %s") % str(e),
                subject=_("Fiscalisation Failed"),
                message_type="comment"
            )
            _logger.exception("Fiscalisation failed for invoice %s", self.name)
            raise UserError(_("Fiscalisation process failed: %s") % str(e))
    
    def _prepare_fiscal_payload(self):
        
        lines = self.env['account.move.line'].search([
            ('move_id', '=', self.id),
            ('quantity', '>', 0)
        ])
        
        receipt_type = self.receipt_type
        
        tax_inclusion_types = set()
        for line in lines:
            if not line.tax_ids:
                continue
                # raise UserError(_(f"Product {line.product_id.name} has no tax configuration."))
            tax_inclusion_types.add(line.tax_ids[0].price_include)
            
        if len(tax_inclusion_types) > 1:
            raise UserError(_("Mixed tax inclusion types detected. All invoice lines must consistently have either tax-inclusive or tax-exclusive prices."))
            
        receiptLinesTaxInclusive = tax_inclusion_types.pop() if tax_inclusion_types else True
        
        return {
            "receipt": {
                "receiptType": self.receipt_type,
                "receiptCurrency": self.currency_id.name,
                "invoiceNo": self.name,
                "buyerData": self._prepare_buyer_data(),
                "receiptNotes": self.ref if self.ref else "",
                "creditDebitNoteInvoiceNo": self.reversed_entry_id.name if self.move_type in ('out_refund', 'in_refund') else "",
                "receiptLinesTaxInclusive": receiptLinesTaxInclusive,
                "receiptLines": self._prepare_receipt_lines(),
                "receiptPayments": self._prepare_payment_data(),
                "receiptTotal": self._adjust_amount(self.amount_total, receipt_type),
                "receiptPrintForm": "InvoiceA4"
            }
        }

    def _prepare_buyer_data(self):
        partner = self.partner_id
        if not partner:
            raise UserError(_("Customer information is required for fiscalisation"))
            
        if partner.vat and partner.tin_number:
            buyer_data = {
                "buyerRegisterName": partner.name,
                "buyerTradeName": partner.commercial_partner_id.name,
                "vatNumber": self.customer_vat,
                "buyerTIN": self.customer_tin,
                "buyerContacts": {
                    "phoneNo": partner.phone or "",
                    "email": partner.email or ""
                },
                "buyerAddress": {
                    "province": partner.state_id.name or "",
                    "city": partner.city or "",
                    "street": partner.street or "",
                    "houseNo": partner.street2 or "",
                    "district": ""
                }
            }
        else:
            buyer_data = None
        
        return buyer_data 

    def _prepare_receipt_lines(self):
        inv_lines = self.env['account.move.line'].search([
            ('move_id', '=', self.id),
            ('quantity', '>', 0),
            ('display_type', 'in', (False, 'product')),  # Only product lines, not section/notes
            ('product_id', '!=', False)                  # Must have an associated product
        ])
        receipt_type = self.receipt_type
        lines = []
        for line in inv_lines:
            # Ensure HS Code is present
            if not line.product_id.hs_code:
                raise UserError(_(f'Product ({line.name}) has no HS Code'))
            
            # Always add a non-discounted sale line
            sale_line_data = {
                "receiptLineType": "Sale",
                "receiptLineNo": len(lines) + 1,
                "receiptLineHSCode": line.product_id.hs_code,
                "receiptLineName": line.name[:100],
                "receiptLinePrice": self._adjust_amount(line.price_unit, receipt_type),
                "receiptLineQuantity": float(line.quantity),
                "receiptLineTotal": self._adjust_amount(line.price_unit * line.quantity, receipt_type),
            }
            if line.tax_ids:
                # Sum tax amounts from all applicable taxes
                sale_line_data["taxPercent"] = float(sum(line.tax_ids.mapped("amount")))
            lines.append(sale_line_data)

            # If the line has a discount, add a separate discount line
            if line.discount > 0:
                discount_amount = line.price_unit * (line.discount / 100.0)
                discount_total = discount_amount * line.quantity
                discount_line_data = {
                    "receiptLineType": "Discount",
                    "receiptLineNo": len(lines) + 1,
                    "receiptLineHSCode": line.product_id.hs_code,
                    "receiptLineName": sale_line_data["receiptLineName"] + " (Discount)",
                    "receiptLinePrice": -self._adjust_amount(discount_amount, receipt_type),
                    "receiptLineQuantity": float(line.quantity),
                    "receiptLineTotal": -self._adjust_amount(discount_total, receipt_type),
                }
                
                # Include tax information for consistent tax calculations
                if "taxPercent" in sale_line_data:
                    discount_line_data["taxPercent"] = sale_line_data["taxPercent"]
                
                lines.append(discount_line_data)
        return lines

    def _prepare_payment_data(self):
        PAYMENT_TYPE_MAPPING = {
            'cash': 'Cash',
            'card': 'Card',
            'bank': 'Transfer',
            'transfer': 'Transfer'
        }
        
        receipt_type = self.receipt_type
        payments = []
        payments.append({
            "moneyTypeCode": 'Cash',
            "paymentAmount": self._adjust_amount(self.amount_total, receipt_type)
        })
        
        return payments

    def _process_fiscal_response(self, response):
        try:
            receipt_date = response.get('receiptFiscalDate')
            fiscal_date = fields.Datetime.now()
            
            if receipt_date:
                try:
                    # Handle ISO format with optional milliseconds
                    receipt_date = receipt_date.split('.')[0]  # Remove milliseconds if present
                    dt = datetime.strptime(receipt_date, "%Y-%m-%dT%H:%M:%S")
                    fiscal_date = fields.Datetime.to_string(dt)
                except ValueError as e:
                    _logger.warning("Date format error: %s, using current date", str(e))
                    
            self.write({
                # 'fiscal_signature': response.get('verificationCode'),
                # 'qr_code': self._generate_qr_code(response.get('qrCodeUrl')),
                'qr_url': response.get('qrCodeUrl'),
                'fiscal_date': fiscal_date,
                'device_id': response.get('deviceID'),
                'receipt_global_number': response.get('receiptGlobalNo'),
                'receipt_number': response.get('receiptNumber'),
                'fiscal_day_no': response.get('fiscalDayNo'),
                'verification_code': response.get('verificationCode'),
                'fiscalised': True
            })
            
        except KeyError as e:
            _logger.error("Missing expected field in API response: %s", str(e))
            raise UserError(_("Invalid response from fiscal device")) from e

    def _show_success_notification(self, response):
        message = _('''
            Fiscalisation Successful!
            Receipt Number: %(number)s
            Verification Code: %(code)s
            Fiscal Date: %(date)s
        ''') % {
            'number': response.get('receiptNumber', 'N/A'),
            'code': response.get('verificationCode', 'N/A'),
            'date': response.get('receiptFiscalDate', 'N/A')
        }
        
        return {
            'type': 'ir.actions.client',
            'tag': 'display_notification',
            'params': {
                'title': _('Success'),
                'message': message,
                'type': 'success',
                'sticky': True,
                'next': {'type': 'ir.actions.act_window_close'},
                'links': [{
                    'label': _('View Receipt'),
                    'url': response.get('qrCodeUrl', '#'),
                    'target': 'new'
                }]
            }
        }
        
    def _adjust_amount(self, amount, receipt_type):
        """Adjust amount sign based on receipt type"""
        multiplier = -1 if receipt_type.upper() == 'CREDITNOTE' else 1
        return float(amount) * multiplier

    def _calculate_total_with_discounts(self):
        """Calculate total including discounts to avoid invoice validation errors."""
        total = 0.0
        for line in self.invoice_line_ids:
            if 'discount' in line.name.lower() or line.discount > 0:
                discount_amount = (line.price_unit * line.quantity) * (line.discount / 100.0)
                total -= discount_amount
            else:
                total += line.price_total
        return total



