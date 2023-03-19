import torch
import torch.nn as nn
import torch.nn.functional as F
from .fusion import TransformerAVFusion

def create_conv(input_channels, output_channels, kernel, paddings, batch_norm=True, Relu=True, stride=1):
    model = [nn.Conv2d(input_channels, output_channels, kernel, stride = stride, padding = paddings)]
    if(batch_norm):
        model.append(nn.BatchNorm2d(output_channels))
    if(Relu):
        model.append(nn.ReLU())
    return nn.Sequential(*model)

def weights_init(m):
    classname = m.__class__.__name__
    if classname.find('Conv') != -1:
        m.weight.data.normal_(0.0, 0.02)
    elif classname.find('BatchNorm2d') != -1:
        m.weight.data.normal_(1.0, 0.02)
        m.bias.data.fill_(0)
    elif classname.find('Linear') != -1:
        m.weight.data.normal_(0.0, 0.02)


class BasicBlock(nn.Module):
    expansion = 1

    def __init__(self, in_planes, planes, stride=1):
        super(BasicBlock, self).__init__()
        self.conv1 = nn.Conv2d(in_planes, planes, kernel_size=3, stride=stride, padding=1, bias=False)
        self.bn1 = nn.BatchNorm2d(planes)
        self.conv2 = nn.Conv2d(planes, planes, kernel_size=3, stride=1, padding=1, bias=False)
        self.bn2 = nn.BatchNorm2d(planes)

        self.shortcut = nn.Sequential()
        if stride != 1 or in_planes != self.expansion*planes:
            self.shortcut = nn.Sequential(
                nn.Conv2d(in_planes, self.expansion*planes, kernel_size=1, stride=stride, bias=False),
                nn.BatchNorm2d(self.expansion*planes)
            )

    def forward(self, x):
        out = F.relu(self.bn1(self.conv1(x)))
        out = self.bn2(self.conv2(out))
        out += self.shortcut(x)
        out = F.relu(out)
        return out


class Resunet(nn.Module):
    def __init__(self, block, num_blocks, n_channels=1, n_classes=1, bilinear=True):
        super(Resunet, self).__init__()
        self.in_planes = 64
        self.n_channels = n_channels
        self.n_classes = n_classes
        self.bilinear = bilinear

        self.inc = DoubleConv(n_channels, 64)
        self.down1 = self._make_layer(block, 128, num_blocks[0], stride=2)
        self.down2 = self._make_layer(block, 256, num_blocks[1], stride=2)
        self.down3 = self._make_layer(block, 512, num_blocks[2], stride=2)
        self.down4 = self._make_layer(block, 512, num_blocks[3], stride=2)

        # fusion module
        self.conv1x1 = create_conv(512, 256, 1, 0)  # reduce dimension of extracted visual features
        self.fusion = TransformerAVFusion()

        self.up1 = Up(1024, 512, bilinear)
        self.up2 = Up(1024, 256, bilinear)
        self.up3 = Up(512, 128, bilinear)
        self.up4 = Up(256, 64, bilinear)
        self.up5 = Up(128, 64, bilinear)
        self.outc = OutConv(64, n_classes)

    def _make_layer(self, block, planes, num_blocks, stride):
        strides = [stride] + [1]*(num_blocks-1)
        layers = []
        for stride in strides:
            layers.append(block(self.in_planes, planes, stride))
            self.in_planes = planes * block.expansion
        return nn.Sequential(*layers)

    def pearson_correlation(self, x, y, eps=1e-8):
        '''
        Arguments
        ---------
        x1 : 4D torch.Tensor audio_feat B X C X H X W
        x2 : 4D torch.Tensor visual_feat
        batch dim first
        '''
        mean_x = torch.mean(x, dim=1, keepdim=True)
        mean_y = torch.mean(y, dim=1, keepdim=True)
        xm = x - mean_x
        ym = y - mean_y
        # dot product

        r_num = torch.sum(torch.mul(xm, ym), dim=1, keepdim=True)
        r_den = torch.norm(xm, 2, dim=1, keepdim=True) * torch.norm(ym, 2, dim=1, keepdim=True)
        r_den_handle = torch.where(r_den == 0, torch.full_like(r_den, eps), r_den)  # avoid division by zero
        r_val = r_num / r_den_handle

        return r_val

    def forward(self, x, visual_feat, sign_feat):
        # print(visual_feat.shape, x.shape)
        # print(1, x.size())
        x1 = self.inc(x)
        # print(2, x1.size())
        # print(2, x1)
        x2 = self.down1(x1)
        # print(3, x2.size())
        # print(3, x2)
        x3 = self.down2(x2)
        # print(4, x3.size())
        # print(4, x3)
        x4 = self.down3(x3)
        # print(5, x4.size())
        # print(5, x4)
        x5 = self.down4(x4)
        # print(6, x5.size())
        # print(6, x5.size())

        visual_feat = self.conv1x1(visual_feat)
        visual_feat = visual_feat.repeat(1, 1, x5.shape[-2]//visual_feat.shape[-2], x5.shape[-1]//visual_feat.shape[-1])

        sign_feat = sign_feat.repeat(1, 1, x5.shape[-2] // sign_feat.shape[-2], x5.shape[-1] // sign_feat.shape[-1])

        vs_feat = torch.cat((visual_feat, sign_feat), dim=1)

        att_feat = self.fusion(vs_feat, x5)

        x = self.up1(x5, att_feat)
        # print(5, x)
        # print(5, x.size())W
        x = self.up2(x, x4)
        # print(4, x)
        # print(4, x.size())
        x = self.up3(x, x3)
        # print(3, x)W
        # print(3, x.size())
        x = self.up4(x, x2)
        # print(2, x)
        # print(2, x.size())
        x = self.up5(x, x1)
        # print(1, x)
        # print(1, x.size())
        # logits = x
        x = self.outc(x)
        return x


class DoubleConv(nn.Module):
    """(convolution => [BN] => ReLU) * 2"""

    def __init__(self, in_channels, out_channels):
        super().__init__()
        self.double_conv = nn.Sequential(
            nn.Conv2d(in_channels, out_channels, kernel_size=3, padding=1),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True),
            nn.Conv2d(out_channels, out_channels, kernel_size=3, padding=1),
            nn.BatchNorm2d(out_channels),
            nn.ReLU(inplace=True)
        )

    def forward(self, x):
        return self.double_conv(x)


# class Down(nn.Module):
#     """Downscaling with maxpool then double conv"""
#
#     def __init__(self, in_channels, out_channels):
#         super().__init__()
#         self.maxpool_conv = nn.Sequential(
#             nn.MaxPool2d(2),
#             DoubleConv(in_channels, out_channels)
#         )
#
#     def forward(self, x):
#         return self.maxpool_conv(x)


class Up(nn.Module):
    """Upscaling then double conv"""

    def __init__(self, in_channels, out_channels, bilinear=True):
        super().__init__()

        # if bilinear, use the normal convolutions to reduce the number of channels
        if bilinear:
            self.up = nn.Upsample(scale_factor=2, mode='bilinear', align_corners=True)
        else:
            self.up = nn.ConvTranspose2d(in_channels // 2, in_channels // 2, kernel_size=2, stride=2)

        self.conv = DoubleConv(in_channels, out_channels)

    def forward(self, x1, x2):
        x1 = self.up(x1)
        # input is CHW
        diffY = x2.size()[2] - x1.size()[2]
        diffX = x2.size()[3] - x1.size()[3]

        x1 = F.pad(x1, [diffX // 2, diffX - diffX // 2,
                        diffY // 2, diffY - diffY // 2])
        # if you have padding issues, see
        # https://github.com/HaiyongJiang/U-Net-Pytorch-Unstructured-Buggy/commit/0e854509c2cea854e247a9c615f175f76fbb2e3a
        # https://github.com/xiaopeng-liao/Pytorch-UNet/commit/8ebac70e633bac59fc22bb5195e513d5832fb3bd
        x = torch.cat([x2, x1], dim=1)
        return self.conv(x)


class OutConv(nn.Module):
    def __init__(self, in_channels, out_channels):
        super(OutConv, self).__init__()
        self.conv = nn.Conv2d(in_channels, out_channels, kernel_size=1)

    def forward(self, x):
        return self.conv(x)


def resunet_middle():
    return Resunet(BasicBlock, [2,2,2,2])


if __name__ == '__main__':
    visual_feat = torch.rand(1, 512, 4, 4)
    sign_feat = torch.rand(1, 256, 1, 1)
    net = resunet_middle()
    y = net(torch.randn(1, 1, 256, 320), visual_feat, sign_feat)
    print(y[0].size())
