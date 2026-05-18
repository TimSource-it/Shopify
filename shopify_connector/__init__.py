from . import models, controllers


def post_init_hook(env):
    # Toegangsrechten aanmaken
    model = env['ir.model'].search([('model', '=', 'shopify.config')], limit=1)
    if model:
        env['ir.model.access'].create({
            'name': 'shopify.config user',
            'model_id': model.id,
            'group_id': env.ref('base.group_user').id,
            'perm_read': True,
            'perm_write': True,
            'perm_create': True,
            'perm_unlink': True,
        })
    
    # Views en menu laden
    import os
    from odoo.tools import convert_file
    module_path = os.path.dirname(__file__)
    for xml_file in ['views/shopify_config_views.xml', 'data/shopify_cron.xml']:
        convert_file(env, 'shopify_connector', xml_file, {}, mode='init', noupdate=False)
