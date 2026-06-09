from odoo import models, fields, api
import logging

_logger = logging.getLogger(__name__)


class ShopifySyncJob(models.Model):
    _name = 'shopify.sync.job'
    _description = 'Shopify Sync Job'
    _order = 'create_date desc'

    config_id = fields.Many2one(
        'shopify.config',
        string='Shopify Shop',
        required=True,
        ondelete='cascade',
    )
    job_type = fields.Selection([
        ('export_product', 'Export product'),
        ('import_order', 'Import order'),
        ('update_inventory', 'Update inventory'),
        ('import_customer', 'Import customer'),
    ], string='Job Type', required=True)
    state = fields.Selection([
        ('pending', 'Queued'),
        ('running', 'Running'),
        ('done', 'Done'),
        ('error', 'Error'),
    ], string='Status', default='pending')
    payload = fields.Text(string='Payload (JSON)')
    error_message = fields.Text(string='Error Message')
    retries = fields.Integer(string='Retries', default=0)
    max_retries = fields.Integer(string='Max Retries', default=3)
    record_id = fields.Integer(string='Record ID')
    record_model = fields.Char(string='Record Model')

    @api.model
    def create_job(self, config_id, job_type, record_model=None, record_id=None, payload=None):
        """Create a new sync job."""
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
