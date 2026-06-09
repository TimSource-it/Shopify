from . import models
from . import controllers


def post_init_hook(env):
    """Set default values after installation."""
    try:
        configs = env['shopify.config'].search([])
        for config in configs:
            _set_defaults(env, config)
    except Exception:
        pass


def _set_defaults(env, config):
    """Set smart defaults for a config record."""
    vals = {}

    if not config.confirm_order_on:
        vals['confirm_order_on'] = 'paid'

    if not config.invoice_policy:
        vals['invoice_policy'] = 'on_confirm'

    if not config.refund_policy:
        vals['refund_policy'] = 'credit_note'

    if 'account.account' in env:
        existing = env['account.account'].search([
            ('name', '=', 'Shopify Payments'),
        ], limit=1)
        if not existing:
            try:
                existing = env['account.account'].create({
                    'name': 'Shopify Payments',
                    'code': '13000',
                    'account_type': 'asset_current',
                })
            except Exception:
                pass
        if existing and not config.account_id:
            vals['account_id'] = existing.id

        if not config.tax_id:
            tax = env['account.tax'].search([
                ('type_tax_use', '=', 'sale'),
                ('active', '=', True),
            ], limit=1)
            if tax:
                vals['tax_id'] = tax.id

    if vals:
        config.write(vals)
