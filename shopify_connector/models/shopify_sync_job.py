from odoo import models, fields, api
import logging

_logger = logging.getLogger(__name__)


class ShopifySyncJob(models.Model):
    _name = 'shopify.sync.job'
    _description = 'Shopify Synchronisatie Taak'
    _order = 'create_date desc'

    config_id = fields.Many2one(
        'shopify.config',
        string='Shopify Winkel',
        required=True,
        ondelete='cascade',
    )
    job_type = fields.Selection([
        ('export_product', 'Product exporteren'),
        ('import_order', 'Bestelling importeren'),
        ('update_inventory', 'Voorraad bijwerken'),
        ('import_customer', 'Klant importeren'),
    ], string='Taak type', required=True)
    state = fields.Selection([
        ('pending', 'In wachtrij'),
        ('running', 'Bezig'),
        ('done', 'Voltooid'),
        ('error', 'Fout'),
    ], string='Status', default='pending')
    payload = fields.Text(string='Payload (JSON)')
    error_message = fields.Text(string='Foutmelding')
    retries = fields.Integer(string='Pogingen', default=0)
    max_retries = fields.Integer(string='Max pogingen', default=3)
    record_id = fields.Integer(string='Record ID')
    record_model = fields.Char(string='Record Model')

    @api.model
    def create_job(self, config_id, job_type, record_model=None, record_id=None, payload=None):
        """Maak een nieuwe sync taak aan."""
        return self.create({
            'config_id': config_id,
            'job_type': job_type,
            'record_model': record_model,
            'record_id': record_id,
            'payload': payload,
        })

    def mark_done(self):
        self.write({'state': 'done'})

    def mark_error(self, message):
        self.write({
            'state': 'error' if self.retries >= self.max_retries else 'pending',
            'error_message': message,
            'retries': self.retries + 1,
        })
