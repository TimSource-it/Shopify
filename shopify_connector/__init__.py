from . import models

def post_init_hook(env):
    env['ir.model.access'].create({
        'name': 'shopify.config user',
        'model_id': env.ref('shopify_connector.model_shopify_config').id,
        'group_id': env.ref('base.group_user').id,
        'perm_read': True,
        'perm_write': True,
        'perm_create': True,
        'perm_unlink': True,
    })
