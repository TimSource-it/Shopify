from . import models, controllers


def post_init_hook(env):
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
