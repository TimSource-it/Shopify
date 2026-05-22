from . import models
from . import controllers

def post_init_hook(env):
    """Stel standaard waarden in na installatie."""
    try:
        configs = env['shopify.config'].search([])
        for config in configs:
            _set_defaults(env, config)
    except Exception:
        pass


def _set_defaults(env, config):
    """Stel slimme defaults in voor een config record."""
    vals = {}

    # Standaard bevestiging instelling
    if not config.confirm_order_on:
        vals['confirm_order_on'] = 'paid'

    # Standaard factuur instelling
    if not config.invoice_policy:
        vals['invoice_policy'] = 'on_confirm'

    # Standaard retour instelling
    if not config.refund_policy:
        vals['refund_policy'] = 'credit_note'

    # Tussenrekening aanmaken als account module beschikbaar is
    if 'account.account' in env:
        existing = env['account.account'].search([
            ('name', '=', 'Shopify Betalingen'),
            ('company_id', '=', env.company.id),
        ], limit=1)
        if not existing:
            try:
                # Zoek een geschikte account type
                account_type = 'asset_current'
                existing = env['account.account'].create({
                    'name': 'Shopify Betalingen',
                    'code': '13000',
                    'account_type': account_type,
                    'company_id': env.company.id,
                })
            except Exception:
                pass
        if existing and not config.account_id:
            vals['account_id'] = existing.id

        # Standaard BTW
        if not config.tax_id:
            tax = env['account.tax'].search([
                ('type_tax_use', '=', 'sale'),
                ('company_id', '=', env.company.id),
                ('active', '=', True),
            ], limit=1)
            if tax:
                vals['tax_id'] = tax.id

    if vals:
        config.write(vals)
