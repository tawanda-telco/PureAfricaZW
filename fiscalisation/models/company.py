from odoo import models, fields

class ResCompany(models.Model):
    _inherit = 'res.company'

    fiscal_device_id = fields.Many2one(
        'fiscal.device', 
        string='Fiscal Device',
        check_company=True
    )