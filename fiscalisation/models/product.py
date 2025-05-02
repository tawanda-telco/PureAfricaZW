from odoo import models, fields

class ProductProduct(models.Model):
    _inherit = 'product.template'

    hs_code = fields.Char(string='HS Code', help='Harmonized System Code for the product')