import os
import cv2
import numpy as np
from pathlib import Path
import sys
import datetime
import io

if sys.platform == 'win32':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

class UIElementSplitter:
    def __init__(self, image_path, output_dir=None):
        self.image_path = image_path
        
        img_array = np.fromfile(image_path, dtype=np.uint8)
        self.image = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
        if self.image is None:
            raise ValueError(f"无法读取图片: {image_path}")
        
        self.height, self.width = self.image.shape[:2]
        
        if output_dir is None:
            base_dir = os.path.dirname(os.path.abspath(image_path))
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            output_dir = os.path.join(base_dir, f"split_elements_{timestamp}")
        self.output_dir = output_dir
        os.makedirs(self.output_dir, exist_ok=True)
        
        self.elements = []
    
    def _non_max_suppression(self, boxes, scores, overlap_threshold=0.3):
        if len(boxes) == 0:
            return []
        
        boxes = np.array(boxes)
        scores = np.array(scores)
        
        x1 = boxes[:, 0]
        y1 = boxes[:, 1]
        x2 = boxes[:, 0] + boxes[:, 2]
        y2 = boxes[:, 1] + boxes[:, 3]
        
        areas = (x2 - x1 + 1) * (y2 - y1 + 1)
        order = scores.argsort()[::-1]
        
        keep = []
        while order.size > 0:
            i = order[0]
            keep.append(i)
            
            xx1 = np.maximum(x1[i], x1[order[1:]])
            yy1 = np.maximum(y1[i], y1[order[1:]])
            xx2 = np.minimum(x2[i], x2[order[1:]])
            yy2 = np.minimum(y2[i], y2[order[1:]])
            
            w = np.maximum(0, xx2 - xx1 + 1)
            h = np.maximum(0, yy2 - yy1 + 1)
            overlap = (w * h) / areas[order[1:]]
            
            inds = np.where(overlap <= overlap_threshold)[0]
            order = order[inds + 1]
        
        return keep
    
    def _detect_by_contours(self, min_area=50, max_area=None):
        if max_area is None:
            max_area = self.width * self.height * 0.1
        
        gray = cv2.cvtColor(self.image, cv2.COLOR_BGR2GRAY)
        
        edges = cv2.Canny(gray, 30, 100)
        kernel = np.ones((2, 2), np.uint8)
        edges = cv2.dilate(edges, kernel, iterations=1)
        
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        boxes = []
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < min_area:
                continue
            if area > max_area:
                continue
            
            x, y, w, h = cv2.boundingRect(contour)
            
            if w < 15 or h < 15:
                continue
            if w > self.width * 0.5 or h > self.height * 0.5:
                continue
            
            boxes.append((x, y, w, h, area))
        
        return boxes
    
    def _detect_by_connected_components(self, min_area=50):
        gray = cv2.cvtColor(self.image, cv2.COLOR_BGR2GRAY)
        
        max_area = self.width * self.height * 0.1
        
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        binary = cv2.bitwise_not(binary)
        
        num_labels, labels, stats, centroids = cv2.connectedComponentsWithStats(binary, connectivity=8)
        
        boxes = []
        for i in range(1, num_labels):
            area = stats[i, cv2.CC_STAT_AREA]
            if area < min_area or area > max_area:
                continue
            
            x = stats[i, cv2.CC_STAT_LEFT]
            y = stats[i, cv2.CC_STAT_TOP]
            w = stats[i, cv2.CC_STAT_WIDTH]
            h = stats[i, cv2.CC_STAT_HEIGHT]
            
            if w < 15 or h < 15:
                continue
            if w > self.width * 0.5 or h > self.height * 0.5:
                continue
            
            boxes.append((x, y, w, h, area))
        
        return boxes
    
    def _detect_text_regions(self):
        gray = cv2.cvtColor(self.image, cv2.COLOR_BGR2GRAY)
        
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
        grad = cv2.morphologyEx(gray, cv2.MORPH_GRADIENT, kernel)
        
        _, binary = cv2.threshold(grad, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        
        kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (15, 3))
        connected = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
        
        contours, _ = cv2.findContours(connected, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        boxes = []
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            
            if w < 30 or h < 12:
                continue
            if w > self.width * 0.6 or h > self.height * 0.3:
                continue
            
            aspect_ratio = w / h
            if aspect_ratio < 1.2:
                continue
            
            boxes.append((x, y, w, h, w * h))
        
        return boxes
    
    def _detect_ui_buttons(self):
        gray = cv2.cvtColor(self.image, cv2.COLOR_BGR2GRAY)
        
        edges = cv2.Canny(gray, 30, 100)
        kernel = np.ones((5, 5), np.uint8)
        edges = cv2.dilate(edges, kernel, iterations=2)
        
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        boxes = []
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < 500:
                continue
            
            x, y, w, h = cv2.boundingRect(contour)
            
            if w < 50 or h < 20:
                continue
            
            aspect_ratio = w / h
            if aspect_ratio < 1.5 or aspect_ratio > 8:
                continue
            
            boxes.append((x, y, w, h, area))
        
        return boxes
    
    def _detect_icons(self, min_size=15, max_size=150):
        gray = cv2.cvtColor(self.image, cv2.COLOR_BGR2GRAY)
        
        edges = cv2.Canny(gray, 40, 120)
        kernel = np.ones((2, 2), np.uint8)
        edges = cv2.dilate(edges, kernel, iterations=1)
        
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        boxes = []
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < min_size * min_size:
                continue
            if area > max_size * max_size:
                continue
            
            x, y, w, h = cv2.boundingRect(contour)
            
            if w < min_size or h < min_size:
                continue
            if w > max_size or h > max_size:
                continue
            
            aspect_ratio = w / h
            if aspect_ratio < 0.3 or aspect_ratio > 3.0:
                continue
            
            boxes.append((x, y, w, h, area))
        
        return boxes
    
    def _detect_by_color_segmentation(self):
        hsv = cv2.cvtColor(self.image, cv2.COLOR_BGR2HSV)
        
        boxes = []
        
        lower_white = np.array([0, 0, 200])
        upper_white = np.array([180, 30, 255])
        mask_white = cv2.inRange(hsv, lower_white, upper_white)
        
        lower_bright = np.array([0, 0, 150])
        upper_bright = np.array([180, 50, 255])
        mask_bright = cv2.inRange(hsv, lower_bright, upper_bright)
        
        for mask in [mask_white, mask_bright]:
            kernel = np.ones((3, 3), np.uint8)
            mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
            mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
            
            contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            
            for contour in contours:
                area = cv2.contourArea(contour)
                if area < 100 or area > self.width * self.height * 0.1:
                    continue
                
                x, y, w, h = cv2.boundingRect(contour)
                
                if w < 20 or h < 15:
                    continue
                if w > self.width * 0.5 or h > self.height * 0.5:
                    continue
                
                boxes.append((x, y, w, h, area))
        
        return boxes
    
    def _detect_ui_elements_by_gradient(self):
        gray = cv2.cvtColor(self.image, cv2.COLOR_BGR2GRAY)
        
        grad_x = cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3)
        grad_y = cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3)
        grad = np.sqrt(grad_x**2 + grad_y**2)
        grad = np.uint8(grad / grad.max() * 255)
        
        _, binary = cv2.threshold(grad, 50, 255, cv2.THRESH_BINARY)
        
        kernel = np.ones((5, 5), np.uint8)
        binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel)
        
        contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        
        boxes = []
        for contour in contours:
            area = cv2.contourArea(contour)
            if area < 200 or area > self.width * self.height * 0.1:
                continue
            
            x, y, w, h = cv2.boundingRect(contour)
            
            if w < 25 or h < 20:
                continue
            if w > self.width * 0.5 or h > self.height * 0.5:
                continue
            
            boxes.append((x, y, w, h, area))
        
        return boxes
    
    def _filter_large_boxes(self, boxes):
        filtered = []
        max_area = self.width * self.height * 0.15
        
        for box in boxes:
            x, y, w, h, area = box
            
            if area > max_area:
                continue
            
            if w > self.width * 0.4 or h > self.height * 0.4:
                continue
            
            filtered.append(box)
        
        return filtered
    
    def _merge_overlapping_boxes(self, boxes, overlap_threshold=0.4):
        if len(boxes) == 0:
            return []
        
        boxes = sorted(boxes, key=lambda b: b[4], reverse=True)
        merged = []
        used = [False] * len(boxes)
        
        for i, box1 in enumerate(boxes):
            if used[i]:
                continue
            
            x1, y1, w1, h1, area1 = box1
            x1_end = x1 + w1
            y1_end = y1 + h1
            
            merged_box = [x1, y1, w1, h1, area1]
            
            for j, box2 in enumerate(boxes[i+1:], i+1):
                if used[j]:
                    continue
                
                x2, y2, w2, h2, area2 = box2
                x2_end = x2 + w2
                y2_end = y2 + h2
                
                overlap_x = max(0, min(x1_end, x2_end) - max(x1, x2))
                overlap_y = max(0, min(y1_end, y2_end) - max(y1, y2))
                overlap_area = overlap_x * overlap_y
                
                box1_area = w1 * h1
                box2_area = w2 * h2
                union_area = box1_area + box2_area - overlap_area
                
                if union_area > 0:
                    overlap_ratio = overlap_area / union_area
                else:
                    overlap_ratio = 0
                
                if overlap_ratio > overlap_threshold:
                    x_min = min(x1, x2)
                    y_min = min(y1, y2)
                    x_max = max(x1_end, x2_end)
                    y_max = max(y1_end, y2_end)
                    
                    merged_box = [x_min, y_min, x_max - x_min, y_max - y_min, union_area]
                    used[j] = True
            
            merged.append(tuple(merged_box))
            used[i] = True
        
        return merged
    
    def detect_all_elements(self):
        all_boxes = []
        
        print("检测轮廓元素...")
        contour_boxes = self._detect_by_contours(min_area=100, max_area=self.width * self.height * 0.1)
        all_boxes.extend(contour_boxes)
        print(f"  找到 {len(contour_boxes)} 个轮廓元素")
        
        print("检测连通组件...")
        component_boxes = self._detect_by_connected_components(min_area=100)
        all_boxes.extend(component_boxes)
        print(f"  找到 {len(component_boxes)} 个连通组件")
        
        print("检测文本区域...")
        text_boxes = self._detect_text_regions()
        all_boxes.extend(text_boxes)
        print(f"  找到 {len(text_boxes)} 个文本区域")
        
        print("检测UI按钮...")
        button_boxes = self._detect_ui_buttons()
        all_boxes.extend(button_boxes)
        print(f"  找到 {len(button_boxes)} 个UI按钮")
        
        print("检测图标...")
        icon_boxes = self._detect_icons(min_size=15, max_size=150)
        all_boxes.extend(icon_boxes)
        print(f"  找到 {len(icon_boxes)} 个图标")
        
        print("检测颜色分割区域...")
        color_boxes = self._detect_by_color_segmentation()
        all_boxes.extend(color_boxes)
        print(f"  找到 {len(color_boxes)} 个颜色区域")
        
        print("检测梯度区域...")
        gradient_boxes = self._detect_ui_elements_by_gradient()
        all_boxes.extend(gradient_boxes)
        print(f"  找到 {len(gradient_boxes)} 个梯度区域")
        
        print(f"\n过滤过大区域...")
        filtered_boxes = self._filter_large_boxes(all_boxes)
        print(f"过滤后剩余 {len(filtered_boxes)} 个元素")
        
        print(f"\n合并重叠区域...")
        merged_boxes = self._merge_overlapping_boxes(filtered_boxes, overlap_threshold=0.4)
        print(f"合并后剩余 {len(merged_boxes)} 个元素")
        
        self.elements = merged_boxes
        return merged_boxes
    
    def save_elements(self, padding=5):
        if not self.elements:
            print("没有检测到元素，请先调用 detect_all_elements()")
            return
        
        saved_count = 0
        for idx, (x, y, w, h, area) in enumerate(self.elements):
            x_start = max(0, x - padding)
            y_start = max(0, y - padding)
            x_end = min(self.width, x + w + padding)
            y_end = min(self.height, y + h + padding)
            
            element_img = self.image[y_start:y_end, x_start:x_end]
            
            if element_img.size == 0:
                continue
            
            filename = f"element_{idx:04d}_{x}_{y}_{w}x{h}.png"
            filepath = os.path.join(self.output_dir, filename)
            
            ext = os.path.splitext(filepath)[1]
            is_success, im_buf_arr = cv2.imencode(ext, element_img)
            if is_success:
                im_buf_arr.tofile(filepath)
            saved_count += 1
        
        print(f"\n已保存 {saved_count} 个元素到: {self.output_dir}")
        return saved_count
    
    def visualize_detections(self, output_path=None):
        if not self.elements:
            print("没有检测到元素，请先调用 detect_all_elements()")
            return None
        
        vis_image = self.image.copy()
        
        for idx, (x, y, w, h, area) in enumerate(self.elements):
            color = (
                (idx * 50) % 255,
                (idx * 100) % 255,
                (idx * 150) % 255
            )
            cv2.rectangle(vis_image, (x, y), (x + w, y + h), color, 2)
            cv2.putText(vis_image, str(idx), (x, y - 5), 
                       cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
        
        if output_path is None:
            output_path = os.path.join(self.output_dir, "detections_visualization.png")
        
        ext = os.path.splitext(output_path)[1]
        is_success, im_buf_arr = cv2.imencode(ext, vis_image)
        if is_success:
            im_buf_arr.tofile(output_path)
        print(f"可视化结果已保存到: {output_path}")
        return vis_image


def main():
    if len(sys.argv) > 1:
        image_path = sys.argv[1]
    else:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        image_path = os.path.join(script_dir, "参考图.png")
    
    if not os.path.exists(image_path):
        print(f"错误: 图片文件不存在: {image_path}")
        if len(sys.argv) == 1:
            input("按回车键退出...")
        return None
    
    print(f"处理图片: {image_path}")
    splitter = UIElementSplitter(image_path)
    
    print(f"图片尺寸: {splitter.width}x{splitter.height}")
    print(f"输出目录: {splitter.output_dir}")
    print("\n开始检测元素...\n")
    
    splitter.detect_all_elements()
    
    print("\n保存元素...")
    saved_count = splitter.save_elements(padding=5)
    
    print("\n生成可视化结果...")
    splitter.visualize_detections()
    
    print(f"\n完成! 共保存 {saved_count} 个元素")
    print(f"输出目录: {splitter.output_dir}")
    print(f"OUTPUT_DIR:{splitter.output_dir}", flush=True)
    
    return splitter.output_dir


if __name__ == "__main__":
    main()
