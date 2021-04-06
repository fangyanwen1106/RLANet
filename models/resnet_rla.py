# This piece of code is developed based on
# https://github.com/pytorch/examples/tree/master/imagenet
import torch
import torch.nn as nn

# import os
# import sys
# change working dir to the current file's directory
# os.chdir(os.path.dirname(sys.argv[0]))
# torch.autograd.set_detect_anomaly(True)

__all__ = ['RLA_ResNet', 'rla_resnet50', 'rla_resnet50_eca',
           'rla_resnet101', 'rla_resnet101_eca',
           'rla_resnet152', 'rla_resnet152_eca',
           'rla_resnext50_32x4d', 'rla_resnext50_32x4d_se', 'rla_resnext50_32x4d_eca',
           'rla_resnext101_32x4d', 'rla_resnext101_32x4d_se', 'rla_resnext101_32x4d_eca'
           ]

model_urls = {
    'resnet50': 'https://download.pytorch.org/models/resnet50-19c8e357.pth',
    'resnet101': 'https://download.pytorch.org/models/resnet101-5d3b4d8f.pth',
    'resnet152': 'https://download.pytorch.org/models/resnet152-b121ed2d.pth',
    'resnext50_32x4d': 'https://download.pytorch.org/models/resnext50_32x4d-7cdf4587.pth',
    'resnext101_32x8d': 'https://download.pytorch.org/models/resnext101_32x8d-8ba56ff5.pth',
}

# RLA channel k: rla_channel = 32 (default)

# https://github.com/moskomule/senet.pytorch/blob/master/senet/se_module.py
class SELayer(nn.Module):
    def __init__(self, channel, reduction=16):
        super(SELayer, self).__init__()
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.fc = nn.Sequential(
            nn.Linear(channel, channel // reduction, bias=False),
            nn.ReLU(inplace=True),
            nn.Linear(channel // reduction, channel, bias=False),
            nn.Sigmoid()
        )

    def forward(self, x):
        b, c, _, _ = x.size()
        y = self.avg_pool(x).view(b, c)
        y = self.fc(y).view(b, c, 1, 1)
        return x * y.expand_as(x)

# from eca_module import eca_layer
class eca_layer(nn.Module):
    """Constructs a ECA module.
    Args:
        channel: Number of channels of the input feature map
        k_size: Adaptive selection of kernel size
        source: https://github.com/BangguWu/ECANet
    """
    def __init__(self, channel, k_size=3):
        super(eca_layer, self).__init__()
        self.avg_pool = nn.AdaptiveAvgPool2d(1)
        self.conv = nn.Conv1d(1, 1, kernel_size=k_size, padding=(k_size - 1) // 2, bias=False) 
        self.sigmoid = nn.Sigmoid()

    def forward(self, x):
        # x: input features with shape [b, c, h, w]
        b, c, h, w = x.size()

        # feature descriptor on the global spatial information
        y = self.avg_pool(x)

        # Two different branches of ECA module
        y = self.conv(y.squeeze(-1).transpose(-1, -2)).transpose(-1, -2).unsqueeze(-1)

        # Multi-scale information fusion
        y = self.sigmoid(y)

        return x * y.expand_as(x)



def conv3x3(in_planes, out_planes, stride=1, groups=1, dilation=1):
    """3x3 convolution with padding"""
    return nn.Conv2d(in_planes, out_planes, kernel_size=3, stride=stride,
                     padding=dilation, groups=groups, bias=False, dilation=dilation)

def conv1x1(in_planes, out_planes, stride=1):
    """1x1 convolution"""
    return nn.Conv2d(in_planes, out_planes, kernel_size=1, stride=stride, bias=False)


#=========================== define bottleneck ============================
class RLA_Bottleneck(nn.Module):
    expansion = 4

    def __init__(self, inplanes, planes, stride=1, downsample=None, 
                 rla_channel=32, SE=False, ECA_size=None, groups=1,
                 base_width=64, dilation=1, norm_layer=None, reduction=16):
        super(RLA_Bottleneck, self).__init__()
        if norm_layer is None:
            norm_layer = nn.BatchNorm2d
        width = int(planes * (base_width / 64.)) * groups
        # Both self.conv2 and self.downsample layers downsample the input when stride != 1
        self.conv1 = conv1x1(inplanes + rla_channel, width)
        self.bn1 = norm_layer(width)
        self.conv2 = conv3x3(width, width, stride, groups, dilation)
        self.bn2 = norm_layer(width)
        self.conv3 = conv1x1(width, planes * self.expansion)
        self.bn3 = norm_layer(planes * self.expansion)
        self.relu = nn.ReLU(inplace=True)
        self.downsample = downsample
        self.stride = stride
        
        self.averagePooling = None
        if downsample is not None and stride != 1:
            self.averagePooling = nn.AvgPool2d((2, 2), stride=(2, 2))
        
        self.se = None
        if SE:
            self.se = SELayer(planes * self.expansion, reduction)
        
        self.eca = None
        if ECA_size != None:
            self.eca = eca_layer(planes * self.expansion, int(ECA_size))

    def forward(self, x, h):
        identity = x
        
        x = torch.cat((x, h), dim=1)

        out = self.conv1(x)
        out = self.bn1(out)
        out = self.relu(out)

        out = self.conv2(out)
        out = self.bn2(out)
        out = self.relu(out)

        out = self.conv3(out)
        out = self.bn3(out)
        
        if self.se != None:
            out = self.se(out)
            
        if self.eca != None:
            out = self.eca(out)
        
        y = out
        
        if self.downsample is not None:
            identity = self.downsample(identity)
        if self.averagePooling is not None:
            h = self.averagePooling(h)
        
        out += identity
        out = self.relu(out)

        return out, y, h


#=========================== define network ============================
class RLA_ResNet(nn.Module):
    '''
    rla_channel: the number of filters of the shared(recurrent) conv in RLA
    SE: whether use SE or not 
    ECA: None: not use ECA, or specify a list of kernel sizes
    '''
    def __init__(self, block, layers, num_classes=1000, 
                 rla_channel=32, SE=False, ECA=None, 
                 zero_init_residual=False,
                 groups=1, width_per_group=64, replace_stride_with_dilation=None,
                 norm_layer=None):
        super(RLA_ResNet, self).__init__()
        if norm_layer is None:
            norm_layer = nn.BatchNorm2d
        self._norm_layer = norm_layer

        self.inplanes = 64
        self.dilation = 1
        if replace_stride_with_dilation is None:
            # each element in the tuple indicates if we should replace
            # the 2x2 stride with a dilated convolution instead
            replace_stride_with_dilation = [False, False, False]
        if len(replace_stride_with_dilation) != 3:
            raise ValueError("replace_stride_with_dilation should be None "
                             "or a 3-element tuple, got {}".format(replace_stride_with_dilation))
        
        if ECA is None:
            ECA = [None] * 4
        elif len(ECA) != 4:
            raise ValueError("argument ECA should be a 4-element tuple, got {}".format(ECA))
        
        self.rla_channel = rla_channel
        self.flops = False
        # flops: whether compute the flops and params or not
        # when use paras_flops, set as True
        self.groups = groups
        self.base_width = width_per_group
        self.conv1 = nn.Conv2d(3, self.inplanes, kernel_size=7, stride=2, padding=3,
                               bias=False)
        self.bn1 = norm_layer(self.inplanes)
        self.relu = nn.ReLU(inplace=True)
        self.maxpool = nn.MaxPool2d(kernel_size=3, stride=2, padding=1)
        
        conv_outs = [None] * 4
        recurrent_convs = [None] * 4
        stages = [None] * 4
        stage_bns = [None] * 4
        
        stages[0], stage_bns[0], conv_outs[0], recurrent_convs[0] = self._make_layer(block, 64, layers[0], 
                                                                                     rla_channel=rla_channel, SE=SE, ECA_size=ECA[0])
        stages[1], stage_bns[1], conv_outs[1], recurrent_convs[1] = self._make_layer(block, 128, layers[1], 
                                                                                     rla_channel=rla_channel, SE=SE, ECA_size=ECA[1], 
                                                                                     stride=2, dilate=replace_stride_with_dilation[0])
        stages[2], stage_bns[2], conv_outs[2], recurrent_convs[2] = self._make_layer(block, 256, layers[2], 
                                                                                     rla_channel=rla_channel, SE=SE, ECA_size=ECA[2], 
                                                                                     stride=2, dilate=replace_stride_with_dilation[1])
        stages[3], stage_bns[3], conv_outs[3], recurrent_convs[3] = self._make_layer(block, 512, layers[3], 
                                                                                     rla_channel=rla_channel, SE=SE, ECA_size=ECA[3], 
                                                                                     stride=2, dilate=replace_stride_with_dilation[2])
        
        self.conv_outs = nn.ModuleList(conv_outs)
        self.recurrent_convs = nn.ModuleList(recurrent_convs)
        self.stages = nn.ModuleList(stages)
        self.stage_bns = nn.ModuleList(stage_bns)
        
        self.tanh = nn.Tanh()
        
        self.bn2 = norm_layer(rla_channel)
        
        self.avgpool = nn.AdaptiveAvgPool2d((1, 1))
        self.fc = nn.Linear(512 * block.expansion + rla_channel, num_classes)

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode='fan_out', nonlinearity='relu')
            elif isinstance(m, (nn.BatchNorm2d, nn.GroupNorm)):
                nn.init.constant_(m.weight, 1)
                nn.init.constant_(m.bias, 0)

        # Zero-initialize the last BN in each residual branch,
        # so that the residual branch starts with zeros, and each residual block behaves like an identity.
        # This improves the model by 0.2~0.3% according to https://arxiv.org/abs/1706.02677
        if zero_init_residual:
            for m in self.modules():
                if isinstance(m, RLA_Bottleneck):
                    nn.init.constant_(m.bn3.weight, 0)
                # elif isinstance(m, RLA_BasicBlock):  # not implemented yet
                #     nn.init.constant_(m.bn2.weight, 0)

    def _make_layer(self, block, planes, blocks, 
                    rla_channel, SE, ECA_size, stride=1, dilate=False):
        
        conv_out = conv1x1(planes * block.expansion, rla_channel)
        recurrent_conv = conv3x3(rla_channel, rla_channel)
        
        norm_layer = self._norm_layer
        downsample = None
        previous_dilation = self.dilation
        if dilate:
            self.dilation *= stride
            stride = 1
        if stride != 1 or self.inplanes != planes * block.expansion:
            downsample = nn.Sequential(
                conv1x1(self.inplanes, planes * block.expansion, stride),
                norm_layer(planes * block.expansion),
            )

        layers = []
        layers.append(block(self.inplanes, planes, stride, downsample, 
                            rla_channel=rla_channel, SE=SE, ECA_size=ECA_size, groups=self.groups,
                            base_width=self.base_width, dilation=previous_dilation, norm_layer=norm_layer))
        self.inplanes = planes * block.expansion
        for _ in range(1, blocks):
            layers.append(block(self.inplanes, planes, 
                                rla_channel=rla_channel, SE=SE, ECA_size=ECA_size, groups=self.groups,
                                base_width=self.base_width, dilation=self.dilation,
                                norm_layer=norm_layer))

        bns = [norm_layer(rla_channel) for _ in range(blocks)]

        return nn.ModuleList(layers), nn.ModuleList(bns), conv_out, recurrent_conv

    def _forward_impl(self, x):
        x = self.conv1(x)
        x = self.bn1(x)
        x = self.relu(x)
        x = self.maxpool(x)

        batch, _, height, width = x.size()
        # self.rla_channel = rla_channel
        if self.flops: # flops = True, then we compute the flops and params of the model
            h = torch.zeros(batch, self.rla_channel, height, width)
        else:
            h = torch.zeros(batch, self.rla_channel, height, width, device='cuda')

        for layers, bns, conv_out, recurrent_conv in zip(self.stages, self.stage_bns, self.conv_outs, self.recurrent_convs):
            for layer, bn in zip(layers, bns):
                x, y, h = layer(x, h)
                
                # RLA module updates
                y_out = conv_out(y)
                h = h + y_out
                h = bn(h)
                h = self.tanh(h)
                h = recurrent_conv(h)
        
        h = self.bn2(h)
        h = self.relu(h)
        
        x = torch.cat((x, h), dim=1)
        
        x = self.avgpool(x)
        x = torch.flatten(x, 1)
        x = self.fc(x)

        return x

    def forward(self, x):
        return self._forward_impl(x)


#=========================== available models ============================
# import torchvision.models as models
# rla_models = dict(
#     rla_resnet50 = RLA_ResNet(RLA_Bottleneck, [3, 4, 6, 3]),
#     rla_resnet50_eca = RLA_ResNet(RLA_Bottleneck, [3, 4, 6, 3], ECA=[3, 3, 3, 3]),
#     rla_resnet50_adeca = RLA_ResNet(RLA_Bottleneck, [3, 4, 6, 3], ECA=[5, 5, 5, 7]),
    
#     rla_resnet101 = RLA_ResNet(RLA_Bottleneck, [3, 4, 23, 3]),
#     rla_resnet101_eca = RLA_ResNet(RLA_Bottleneck, [3, 4, 23, 3], ECA=[3, 3, 3, 3]),
#     rla_resnet101_adeca = RLA_ResNet(RLA_Bottleneck, [3, 4, 23, 3], ECA=[5, 5, 5, 7]),
    
#     rla_resnet152 = RLA_ResNet(RLA_Bottleneck, [3, 8, 36, 3]),
#     rla_resnet152_eca = RLA_ResNet(RLA_Bottleneck, [3, 8, 36, 3], ECA=[3, 3, 3, 3]),
#     rla_resnet152_adeca = RLA_ResNet(RLA_Bottleneck, [3, 8, 36, 3], ECA=[5, 5, 5, 7]),
    
#     rla_resnext50_32x4d = RLA_ResNet(RLA_Bottleneck, [3, 4, 6, 3], groups=32, width_per_group=4),
#     rla_resnext50_32x4d_se = RLA_ResNet(RLA_Bottleneck, [3, 4, 6, 3], SE=True, groups=32, width_per_group=4),
#     rla_resnext50_32x4d_eca = RLA_ResNet(RLA_Bottleneck, [3, 4, 6, 3], ECA=[3, 3, 3, 3], groups=32, width_per_group=4),
#     rla_resnext50_32x4d_adeca = RLA_ResNet(RLA_Bottleneck, [3, 4, 6, 3], ECA=[5, 5, 5, 7], groups=32, width_per_group=4),
    
#     rla_resnext101_32x4d = RLA_ResNet(RLA_Bottleneck, [3, 4, 23, 3], groups=32, width_per_group=4),
#     rla_resnext101_32x4d_se = RLA_ResNet(RLA_Bottleneck, [3, 4, 23, 3], SE=True, groups=32, width_per_group=4),
#     rla_resnext101_32x4d_eca = RLA_ResNet(RLA_Bottleneck, [3, 4, 23, 3], ECA=[3, 3, 3, 3], groups=32, width_per_group=4),
#     rla_resnext101_32x4d_adeca = RLA_ResNet(RLA_Bottleneck, [3, 4, 23, 3], ECA=[5, 5, 5, 7], groups=32, width_per_group=4),
# )


def rla_resnet50(rla_channel=32):
    """ Constructs a RLA_ResNet-50 model.
    default: 
        num_classes=1000, rla_channel=32, SE=False, ECA=None
    """
    print("Constructing rla_resnet50......")
    model = RLA_ResNet(RLA_Bottleneck, [3, 4, 6, 3])
    return model

def rla_resnet50_eca(rla_channel=32, k_size=[5, 5, 5, 7]):
    """Constructs a RLA_ResNet-50_ECA model.
    Args:
        k_size: Adaptive selection of kernel size
        rla_channel: the number of filters of the shared(recurrent) conv in RLA
    """
    print("Constructing rla_resnet50_eca......")
    model = RLA_ResNet(RLA_Bottleneck, [3, 4, 6, 3], rla_channel=rla_channel, ECA=k_size)
    return model


def rla_resnet101(rla_channel=32):
    """ Constructs a RLA_ResNet-101 model.
    default: 
        num_classes=1000, rla_channel=32, SE=False, ECA=None
    """
    print("Constructing rla_resnet101......")
    model = RLA_ResNet(RLA_Bottleneck, [3, 4, 23, 3])
    return model

def rla_resnet101_eca(rla_channel=32, k_size=[5, 5, 5, 7]):
    """Constructs a RLA_ResNet-101_ECA model.
    Args:
        k_size: Adaptive selection of kernel size
        rla_channel: the number of filters of the shared(recurrent) conv in RLA
    """
    print("Constructing rla_resnet101_eca......")
    model = RLA_ResNet(RLA_Bottleneck, [3, 4, 23, 3], ECA=k_size)
    return model


def rla_resnet152(rla_channel=32):
    """ Constructs a RLA_ResNet-152 model.
    default: 
        num_classes=1000, rla_channel=32, SE=False, ECA=None
    """
    print("Constructing rla_resnet152......")
    model = RLA_ResNet(RLA_Bottleneck, [3, 8, 36, 3])
    return model

def rla_resnet152_eca(rla_channel=32, k_size=[5, 5, 5, 7]):
    """Constructs a RLA_ResNet-101_ECA model.
    Args:
        k_size: Adaptive selection of kernel size
        rla_channel: the number of filters of the shared(recurrent) conv in RLA
    """
    print("Constructing rla_resnet101_eca......")
    model = RLA_ResNet(RLA_Bottleneck, [3, 8, 36, 3], ECA=k_size)
    return model


def rla_resnext50_32x4d(rla_channel=32):
    """ Constructs a RLA_ResNeXt50_32x4d model.
    default: 
        num_classes=1000, rla_channel=32, SE=False, ECA=None
    """
    print("Constructing rla_resnext50_32x4d......")
    model = RLA_ResNet(RLA_Bottleneck, [3, 4, 6, 3], groups=32, width_per_group=4)
    return model

def rla_resnext50_32x4d_se(rla_channel=32):
    """ Constructs a RLA_ResNeXt50_32x4d_SE model.
    default: 
        num_classes=1000, rla_channel=32, SE=False, ECA=None
    """
    print("Constructing rla_resnext50_32x4d_se......")
    model = RLA_ResNet(RLA_Bottleneck, [3, 4, 6, 3], SE=True, groups=32, width_per_group=4)
    return model

def rla_resnext50_32x4d_eca(rla_channel=32, k_size=[5, 5, 5, 7]): 
    """Constructs a RLA_ResNeXt50_32x4d_ECA model.
    Args:
        k_size: Adaptive selection of kernel size
        rla_channel: the number of filters of the shared(recurrent) conv in RLA
    """
    print("Constructing rla_resnext50_32x4d_eca......")
    model = RLA_ResNet(RLA_Bottleneck, [3, 4, 6, 3], ECA=k_size, groups=32, width_per_group=4)
    return model


def rla_resnext101_32x4d(rla_channel=32):
    """ Constructs a RLA_ResNeXt101_32x4d model.
    default: 
        num_classes=1000, rla_channel=32, SE=False, ECA=None
    """
    print("Constructing rla_resnext101_32x4d......")
    model = RLA_ResNet(RLA_Bottleneck, [3, 4, 23, 3], groups=32, width_per_group=4)
    return model

def rla_resnext101_32x4d_se(rla_channel=32):
    """ Constructs a RLA_ResNeXt101_32x4d_SE model.
    default: 
        num_classes=1000, rla_channel=32, SE=False, ECA=None
    """
    print("Constructing rla_resnext101_32x4d_se......")
    model = RLA_ResNet(RLA_Bottleneck, [3, 4, 23, 3], SE=True, groups=32, width_per_group=4)
    return model

def rla_resnext101_32x4d_eca(rla_channel=32, k_size=[5, 5, 5, 7]): 
    """Constructs a RLA_ResNeXt101_32x4d_ECA model.
    Args:
        k_size: Adaptive selection of kernel size
        rla_channel: the number of filters of the shared(recurrent) conv in RLA
    """
    print("Constructing rla_resnext101_32x4d_eca......")
    model = RLA_ResNet(RLA_Bottleneck, [3, 4, 23, 3], ECA=k_size, groups=32, width_per_group=4)
    return model
