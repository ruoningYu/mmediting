import os.path as osp

import mmcv
import numpy as np
import torch
from mmedit.core import tensor2img

from ..base import BaseModel
from ..builder import build_backbone, build_component, build_loss
from ..common import set_requires_grad
from ..registry import MODELS


@MODELS.register_module()
class Pix2Pix(BaseModel):
    """Pix2Pix model for paired image-to-image translation.

    Ref:
    Image-to-Image Translation with Conditional Adversarial Networks

    Args:
        generator (dict): Config for the generator.
        discriminator (dict): Config for the discriminator.
        gan_loss (dict): Config for the gan loss.
        pixel_loss (dict): Config for the pixel loss. Default: None.
        train_cfg (dict): Config for training. Default: None.
            You may change the training of gan by setting:
            `disc_steps`: how many discriminator updates after one generator
            update.
            `disc_init_steps`: how many discriminator updates at the start of
            the training.
            These two keys are useful when training with WGAN.
            `direction`: image-to-image translation direction (the model
            training direction): a2b | b2a.
        test_cfg (dict): Config for testing. Default: None.
            You may change the testing of gan by setting:
            `direction`: image-to-image translation direction (the model
            training direction, same as testing direction): a2b | b2a.
            `show_input`: whether to show input real images.
        pretrained (str): Path for pretrained model. Default: None.
    """

    def __init__(self,
                 generator,
                 discriminator,
                 gan_loss,
                 pixel_loss=None,
                 train_cfg=None,
                 test_cfg=None,
                 pretrained=None):
        super(Pix2Pix, self).__init__()

        self.train_cfg = train_cfg
        self.test_cfg = test_cfg

        # generator
        self.generator = build_backbone(generator)
        # discriminator
        self.discriminator = build_component(discriminator)

        # losses
        assert gan_loss is not None  # gan loss cannot be None
        self.gan_loss = build_loss(gan_loss)
        self.pixel_loss = build_loss(pixel_loss) if pixel_loss else None

        self.disc_steps = 1 if self.train_cfg is None else self.train_cfg.get(
            'disc_steps', 1)
        self.disc_init_steps = (0 if self.train_cfg is None else
                                self.train_cfg.get('disc_init_steps', 0))
        if self.train_cfg is None:
            self.direction = ('a2b' if self.test_cfg is None else
                              self.test_cfg.get('direction', 'a2b'))
        else:
            self.direction = self.train_cfg.get('direction', 'a2b')
        self.step_counter = 0  # counting training steps

        self.show_input = (False if self.test_cfg is None else
                           self.test_cfg.get('show_input', False))

        self.init_weights(pretrained)

    def init_weights(self, pretrained=None):
        self.generator.init_weights(pretrained=pretrained)
        self.discriminator.init_weights(pretrained=pretrained)

    def setup(self, img_a, img_b, meta):
        # perform necessary pre-processing steps
        a2b = self.direction == 'a2b'
        real_a = img_a if a2b else img_b
        real_b = img_b if a2b else img_a
        image_path = [v['img_a_path' if a2b else 'img_b_path'] for v in meta]

        return real_a, real_b, image_path

    def forward_train(self, img_a, img_b, meta):
        # necessary setup
        real_a, real_b, image_path = self.setup(img_a, img_b, meta)
        fake_b = self.generator(real_a)
        results = dict(real_a=real_a, fake_b=fake_b, real_b=real_b)
        return results

    def forward_test(self,
                     img_a,
                     img_b,
                     meta,
                     save_image=False,
                     save_path=None,
                     iteration=None):
        # No need for metrics during training for pix2pix. And
        # this is a special trick in pix2pix original paper & implementation,
        # collecting the statistics of the test batch at test time.
        self.train()

        # necessary setup
        real_a, real_b, image_path = self.setup(img_a, img_b, meta)

        fake_b = self.generator(real_a)
        results = dict(
            real_a=real_a.cpu(), fake_b=fake_b.cpu(), real_b=real_b.cpu())

        # save image
        if save_image:
            assert save_path is not None
            folder_name = osp.splitext(osp.basename(image_path[0]))[0]
            if self.show_input:
                if iteration:
                    save_path = osp.join(
                        save_path, folder_name,
                        f'{folder_name}-{iteration + 1:06d}-ra-fb-rb.png')
                else:
                    save_path = osp.join(save_path,
                                         f'{folder_name}-ra-fb-rb.png')
                output = np.concatenate([
                    tensor2img(results['real_a'], min_max=(-1, 1)),
                    tensor2img(results['fake_b'], min_max=(-1, 1)),
                    tensor2img(results['real_b'], min_max=(-1, 1))
                ],
                                        axis=1)
            else:
                if iteration:
                    save_path = osp.join(
                        save_path, folder_name,
                        f'{folder_name}-{iteration + 1:06d}-fb.png')
                else:
                    save_path = osp.join(save_path, f'{folder_name}-fb.png')
                output = tensor2img(results['fake_b'], min_max=(-1, 1))
            flag = mmcv.imwrite(output, save_path)
            results['saved_flag'] = flag

        return results

    def forward_dummy(self, img):
        """Used for computing network FLOPs.

        Args:
            img (Tensor): Dummy input used to compute FLOPs.

        Returns:
            Tensor: Dummy output produced by forwarding the dummy input.
        """
        out = self.generator(img)
        return out

    def forward(self, img_a, img_b, meta, test_mode=False, **kwargs):
        """Forward function.

        Args:
            img_a (Tensor): Input image from domain A.
            img_b (Tensor): Input image from domain B.
            meta (lst[dict]): Input meta data.
            test_mode (bool): Whether in test mode or not. Default: False.
            kwargs (dict): Other arguments.
        """
        if not test_mode:
            return self.forward_train(img_a, img_b, meta)
        else:
            return self.forward_test(img_a, img_b, meta, **kwargs)

    def backward_discriminator(self, outputs):
        # GAN loss for the discriminator
        losses = dict()
        # conditional GAN
        fake_ab = torch.cat((outputs['real_a'], outputs['fake_b']), 1)
        fake_pred = self.discriminator(fake_ab.detach())
        losses['loss_gan_d_fake'] = self.gan_loss(
            fake_pred, target_is_real=False, is_disc=True)
        real_ab = torch.cat((outputs['real_a'], outputs['real_b']), 1)
        real_pred = self.discriminator(real_ab)
        losses['loss_gan_d_real'] = self.gan_loss(
            real_pred, target_is_real=True, is_disc=True)

        loss_d, log_vars_d = self.parse_losses(losses)
        loss_d *= 0.5
        loss_d.backward()
        return log_vars_d

    def backward_generator(self, outputs):
        losses = dict()
        # GAN loss for the generator
        fake_ab = torch.cat((outputs['real_a'], outputs['fake_b']), 1)
        fake_pred = self.discriminator(fake_ab)
        losses['loss_gan_g'] = self.gan_loss(
            fake_pred, target_is_real=True, is_disc=False)
        # pixel loss for the generator
        if self.pixel_loss:
            losses['loss_pixel'] = self.pixel_loss(outputs['fake_b'],
                                                   outputs['real_b'])

        loss_g, log_vars_g = self.parse_losses(losses)
        loss_g.backward()
        return log_vars_g

    def train_step(self, data_batch, optimizer):
        # data
        img_a = data_batch['img_a']
        img_b = data_batch['img_b']
        meta = data_batch['meta']

        # forward generator
        outputs = self.forward(img_a, img_b, meta, test_mode=False)

        log_vars = dict()

        # discriminator
        set_requires_grad(self.discriminator, True)
        # optimize
        optimizer['discriminator'].zero_grad()
        log_vars.update(self.backward_discriminator(outputs=outputs))
        optimizer['discriminator'].step()

        # generator, no updates to discriminator parameters.
        if (self.step_counter % self.disc_steps == 0
                and self.step_counter >= self.disc_init_steps):
            set_requires_grad(self.discriminator, False)
            # optimize
            optimizer['generator'].zero_grad()
            log_vars.update(self.backward_generator(outputs=outputs))
            optimizer['generator'].step()

        self.step_counter += 1

        log_vars.pop('loss', None)  # remove the unnecessary 'loss'
        results = dict(
            log_vars=log_vars,
            num_samples=len(outputs['real_a']),
            results=dict(
                real_a=outputs['real_a'].cpu(),
                fake_b=outputs['fake_b'].cpu(),
                real_b=outputs['real_b'].cpu()))

        return results

    def val_step(self, data_batch, **kwargs):
        # data
        img_a = data_batch['img_a']
        img_b = data_batch['img_b']
        meta = data_batch['meta']

        # forward generator
        results = self.forward(img_a, img_b, meta, test_mode=True, **kwargs)
        return results
