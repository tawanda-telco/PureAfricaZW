# -*- coding: utf-8 -*-
# from odoo import http


# class Fiscalisation(http.Controller):
#     @http.route('/fiscalisation/fiscalisation', auth='public')
#     def index(self, **kw):
#         return "Hello, world"

#     @http.route('/fiscalisation/fiscalisation/objects', auth='public')
#     def list(self, **kw):
#         return http.request.render('fiscalisation.listing', {
#             'root': '/fiscalisation/fiscalisation',
#             'objects': http.request.env['fiscalisation.fiscalisation'].search([]),
#         })

#     @http.route('/fiscalisation/fiscalisation/objects/<model("fiscalisation.fiscalisation"):obj>', auth='public')
#     def object(self, obj, **kw):
#         return http.request.render('fiscalisation.object', {
#             'object': obj
#         })

