import torch
import numpy as np
import torch.nn.functional as F

from torchvision.models import alexnet
from torch.autograd import Variable
from torch import nn

from .config import config
from pose.models.pose_resnet import get_pose_net
from pose.core.config import config as cfg
from pose.core.config import update_config

class SiameseAlexNet(nn.Module):
	def __init__(self, gpu_id, train=True):
		self.cfg_file='/export/home/zby/SiamFC/cfgs/pose_res152.yaml'
		update_config(self.cfg_file)
		super(SiameseAlexNet, self).__init__()
		self.PoseNet = get_pose_net(cfg, is_train=False, flag = 4)
		self.exemplar_size = (8,8)
		self.multi_instance_size = [(24,24),(22,22)]
		
		self.features = nn.Sequential(
			nn.Conv2d(3, 96, 11, 2),
			nn.BatchNorm2d(96),
			nn.ReLU(inplace=True),
			nn.MaxPool2d(3, 2),
			nn.Conv2d(96, 256, 5, 1, groups=2),
			nn.BatchNorm2d(256),
			nn.ReLU(inplace=True),
			nn.MaxPool2d(3, 2),
			nn.Conv2d(256, 384, 3, 1),
			nn.BatchNorm2d(384),
			nn.ReLU(inplace=True),
			nn.Conv2d(384, 384, 3, 1, groups=2),
			nn.BatchNorm2d(384),
			nn.ReLU(inplace=True))
			
		self.conv_pose = nn.Conv2d(2048, 384, 3, 1, padding=1)
		self.conv_final = nn.Conv2d(384, 256, 3, 1, groups=2)
		
		self.corr_bias = nn.Parameter(torch.zeros(1))
		if train:
			gt, weight = self._create_gt_mask((config.train_response_sz, config.train_response_sz),mode='train')
			with torch.cuda.device(gpu_id):
				self.train_gt = torch.from_numpy(gt).cuda()
				self.train_weight = torch.from_numpy(weight).cuda()
			gt, weight = self._create_gt_mask((config.response_sz, config.response_sz), mode='valid')
			with torch.cuda.device(gpu_id):
				self.valid_gt = torch.from_numpy(gt).cuda()
				self.valid_weight = torch.from_numpy(weight).cuda()
		self.exemplar = None

	def init_models(self, pose_model_file = '/export/home/zby/SiamFC/data/models/final_new.pth.tar', track_model_file = '/export/home/zby/SiamFC/models/siamfc_pretrained.pth'):
		pose_model_file = cfg.TEST.MODEL_FILE if pose_model_file is None else pose_model_file
		print("PoseEstimation: Loading checkpoint from %s" % (pose_model_file))
		checkpoint=torch.load(pose_model_file)
		from collections import OrderedDict
		model_dict = self.PoseNet.state_dict()
		new_state_dict = OrderedDict()
		for k,v in checkpoint.items():
			new_name = k[7:] if 'module' in k else k
			if new_name in model_dict:
				new_state_dict[new_name]=v
		model_dict.update(new_state_dict)
		self.PoseNet.load_state_dict(model_dict)
		print('PoseEstimation: PoseEstimation network has been initilized')	
		print('-----------------------------------------------------------')
		
		
		# for m in self.features.modules():
			# if isinstance(m, nn.Conv2d):
				# nn.init.kaiming_normal_(m.weight.data, mode='fan_out', nonlinearity='relu')
			# elif isinstance(m, nn.BatchNorm2d):
				# m.weight.data.fill_(1)
				# m.bias.data.zero_()
		print("Tracking: Loading checkpoint from %s" % (track_model_file))
		checkpoint=torch.load(track_model_file)
		model_dict = self.features.state_dict()
		new_state_dict = OrderedDict()
		for k,v in checkpoint.items():
			new_name = k[9:] if 'feature' in k else k
			if new_name in model_dict:
				new_state_dict[new_name]=v
		model_dict.update(new_state_dict)
		self.features.load_state_dict(model_dict)
		print('Tracking: Tracking network has been initilized')	
		print('-----------------------------------------------------------')
				
		for p in self.PoseNet.parameters():
			p.requires_grad = False
			
	def set_bn_fix(self):
		def set_bn_eval(m):
			classname = m.__class__.__name__
			if classname.find('BatchNorm') != -1:
			  m.eval()
		self.PoseNet.apply(set_bn_eval)
		
	def forward(self, x =(None, None), y = (None,None), feature = None):
		H = W = 239
		exemplar, instance = x
		exemplar_pose, instance_pose = y
		if instance is not None:
			N, C, H, W = instance.shape
		if H == 239:
			self.instance_size = self.multi_instance_size[1]
		elif H == 255:
			self.instance_size = self.multi_instance_size[0]
		
		if feature is None:
			if exemplar is not None and instance is not None:
				batch_size = exemplar.shape[0]
				
				exemplar_pose_feature = self.PoseNet(exemplar_pose, flag=3)
				instance_pose_feature = self.PoseNet(instance_pose, flag=3)
				#print(exemplar_pose_feature.shape, instance_pose_feature.shape)
				exemplar_pose_feature = F.upsample(exemplar_pose_feature, size= self.exemplar_size, mode='bilinear')
				instance_pose_feature = F.upsample(instance_pose_feature, size= self.instance_size, mode='bilinear')
				#print(exemplar_pose_feature.shape, instance_pose_feature.shape)
				
				exemplar = self.conv_final(self.features(exemplar) + self.conv_pose(exemplar_pose_feature))
				instance = self.conv_final(self.features(instance) + self.conv_pose(instance_pose_feature))
				
				score_map = []
				#print(instance.shape)
				if N > 1:
					for i in range(N):
						score = F.conv2d(instance[i:i+1], exemplar[i:i+1]) * config.response_scale + self.corr_bias
						score_map.append(score)
					return torch.cat(score_map, dim=0)
				else:
					return F.conv2d(instance, exemplar) * config.response_scale + self.bias
			elif exemplar is not None and instance is None:
				exemplar_pose_feature = self.PoseNet(exemplar_pose, flag=3)
				exemplar_pose_feature = F.upsample(exemplar_pose_feature, size= self.exemplar_size, mode='bilinear')
				exemplar = self.conv_final(self.features(exemplar) + self.conv_pose(exemplar_pose_feature))
				# inference used
				#self.exemplar = self.features(exemplar)
				return exemplar
			else:
				# inference used we don't need to scale the reponse or add bias
				instance_pose_feature = self.PoseNet(instance_pose, flag=3)
				instance_pose_feature = F.upsample(instance_pose_feature, size= self.instance_size, mode='bilinear')
				instance = self.conv_final(self.features(instance) + self.conv_pose(instance_pose_feature))
				score_map = []
				for i in range(instance.shape[0]):
					score_map.append(F.conv2d(instance[i:i+1], self.exemplar))
				return torch.cat(score_map, dim=0)
		else:
			self.exemplar = feature
			instance_pose_feature = self.PoseNet(instance_pose, flag=3)
			self.instance_size
			instance_pose_feature = F.upsample(instance_pose_feature, size= self.instance_size, mode='bilinear')
			#print(instance_pose_feature.shape)
			instance = self.conv_final(self.features(instance) + self.conv_pose(instance_pose_feature))
			score_map = []
			for i in range(instance.shape[0]):
				score_map.append(F.conv2d(instance[i:i+1], self.exemplar))
			return torch.cat(score_map, dim=0)		

	def loss(self, pred):
		return F.binary_cross_entropy_with_logits(pred, self.gt)

	def weighted_loss(self, pred):
		if self.training:
			#print(pred.shape,self.train_gt.shape)
			return F.binary_cross_entropy_with_logits(pred, self.train_gt,
					self.train_weight, size_average = False) / config.train_batch_size # normalize the batch_size
		else:
			#print(pred.shape, self.valid_gt.shape, self.valid_weight.shape)
			return F.binary_cross_entropy_with_logits(pred, self.valid_gt,
					self.valid_weight, size_average = False) / config.valid_batch_size # normalize the batch_size

	def _create_gt_mask(self, shape, mode='train'):
		# same for all pairs
		h, w = shape
		y = np.arange(h, dtype=np.float32) - (h-1) / 2.
		x = np.arange(w, dtype=np.float32) - (w-1) / 2.
		y, x = np.meshgrid(y, x)
		dist = np.sqrt(x**2 + y**2)
		mask = np.zeros((h, w))
		mask[dist <= config.radius / config.total_stride] = 1
		mask = mask[np.newaxis, :, :]
		weights = np.ones_like(mask)
		weights[mask == 1] = 0.5 / np.sum(mask == 1)
		weights[mask == 0] = 0.5 / np.sum(mask == 0)
		if mode == 'train':
			mask = np.repeat(mask, config.train_batch_size, axis=0)[:, np.newaxis, :, :]
		elif mode == 'valid':
			mask = np.repeat(mask, config.valid_batch_size, axis=0)[:, np.newaxis, :, :]
		return mask.astype(np.float32), weights.astype(np.float32)
	