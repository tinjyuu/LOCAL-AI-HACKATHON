import os
import torch
from concurrent.futures import ProcessPoolExecutor
from datasets import load_dataset
import numpy as np
import whisper
import argparse
import multiprocessing as mp
import logging
import json
import shutil
import glob
import warnings
import time

warnings.filterwarnings("ignore", category=UserWarning, module="torch.nn.utils.weight_norm")

# ロガーの作成
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ログのフォーマットを設定
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(filename)s:%(lineno)d - %(message)s')

# コンソールハンドラを作成し、ロガーに追加
console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# コマンドライン引数のパーサーを作成
parser = argparse.ArgumentParser(description='Audio analysis and transcription')
parser.add_argument('--start', type=int, default=0, help='Starting index of the dataset (default: 0)')
parser.add_argument('--end', type=int, default=None, help='Ending index of the dataset (default: None, process all items)')
parser.add_argument('--snr_threshold', type=float, default=100.0, help='SNR threshold for filtering (default: 100.0)')
parser.add_argument('--score_threshold', type=float, default=3.0, help='Score threshold for filtering (default: 3.0)')
parser.add_argument('--batch_size', type=int, default=100000, help='Batch size for processing (default: 100000)')
parser.add_argument('--data_dir', type=str, default='data', help='Directory to store output data (default: data)')
parser.add_argument('--skip_whisper', action="store_true")
parser.add_argument('--dataset_name', type=str, default='ja000')

args = parser.parse_args()

snr_threshold = args.snr_threshold
score_threshold = args.score_threshold
dataset_name = args.dataset_name

num_gpus = torch.cuda.device_count()

logger.info(f"skip_whisper: {args.skip_whisper}")

# データを前処理するための関数
def preprocess_audio(data):
    # データが整数型の場合、浮動小数点型に変換
    if data.dtype == np.int16:
        data = data.astype(np.float32) / np.iinfo(np.int16).max
    elif data.dtype == np.int32:
        data = data.astype(np.float32) / np.iinfo(np.int32).max

    # ステレオをモノラルに変換（必要があれば）
    if len(data.shape) == 2:
        data = data.mean(axis=1)

    return data

def wada_snr(wav):
    # Direct blind estimation of the SNR of a speech signal.
    #
    # Paper on WADA SNR:
    #   http://www.cs.cmu.edu/~robust/Papers/KimSternIS08.pdf
    #
    # This function was adapted from this matlab code:
    #   https://labrosa.ee.columbia.edu/projects/snreval/#9

    # init
    eps = 1e-10
    # next 2 lines define a fancy curve derived from a gamma distribution -- see paper
    db_vals = np.arange(-20, 101)
    g_vals = np.array([0.40974774, 0.40986926, 0.40998566, 0.40969089, 0.40986186, 0.40999006, 0.41027138, 0.41052627, 0.41101024, 0.41143264, 0.41231718, 0.41337272, 0.41526426, 0.4178192 , 0.42077252, 0.42452799, 0.42918886, 0.43510373, 0.44234195, 0.45161485, 0.46221153, 0.47491647, 0.48883809, 0.50509236, 0.52353709, 0.54372088, 0.56532427, 0.58847532, 0.61346212, 0.63954496, 0.66750818, 0.69583724, 0.72454762, 0.75414799, 0.78323148, 0.81240985, 0.84219775, 0.87166406, 0.90030504, 0.92880418, 0.95655449, 0.9835349 , 1.01047155, 1.0362095 , 1.06136425, 1.08579312, 1.1094819 , 1.13277995, 1.15472826, 1.17627308, 1.19703503, 1.21671694, 1.23535898, 1.25364313, 1.27103891, 1.28718029, 1.30302865, 1.31839527, 1.33294817, 1.34700935, 1.3605727 , 1.37345513, 1.38577122, 1.39733504, 1.40856397, 1.41959619, 1.42983624, 1.43958467, 1.44902176, 1.45804831, 1.46669568, 1.47486938, 1.48269965, 1.49034339, 1.49748214, 1.50435106, 1.51076426, 1.51698915, 1.5229097 , 1.528578  , 1.53389835, 1.5391211 , 1.5439065 , 1.54858517, 1.55310776, 1.55744391, 1.56164927, 1.56566348, 1.56938671, 1.57307767, 1.57654764, 1.57980083, 1.58304129, 1.58602496, 1.58880681, 1.59162477, 1.5941969 , 1.59693155, 1.599446  , 1.60185011, 1.60408668, 1.60627134, 1.60826199, 1.61004547, 1.61192472, 1.61369656, 1.61534074, 1.61688905, 1.61838916, 1.61985374, 1.62135878, 1.62268119, 1.62390423, 1.62513143, 1.62632463, 1.6274027 , 1.62842767, 1.62945532, 1.6303307 , 1.63128026, 1.63204102])

    # peak normalize, get magnitude, clip lower bound
    wav = np.array(wav)
    if wav.size == 0:
        return np.nan
    wav = wav / abs(wav).max()
    abs_wav = abs(wav)
    abs_wav[abs_wav < eps] = eps

    # calcuate statistics
    # E[|z|]
    v1 = max(eps, abs_wav.mean())
    # E[log|z|]
    v2 = np.log(abs_wav).mean()
    # log(E[|z|]) - E[log(|z|)]
    v3 = np.log(v1) - v2

    # table interpolation
    wav_snr_idx = None
    if any(g_vals < v3):
        wav_snr_idx = np.where(g_vals < v3)[0].max()
    # handle edge cases or interpolate
    if wav_snr_idx is None:
        wav_snr = db_vals[0]
    elif wav_snr_idx == len(db_vals) - 1:
        wav_snr = db_vals[-1]
    else:
        wav_snr = db_vals[wav_snr_idx] + \
            (v3-g_vals[wav_snr_idx]) / (g_vals[wav_snr_idx+1] - \
            g_vals[wav_snr_idx]) * (db_vals[wav_snr_idx+1] - db_vals[wav_snr_idx])

    # Calculate SNR
    dEng = sum(wav**2)
    dFactor = 10**(wav_snr / 10)
    dNoiseEng = dEng / (1 + dFactor) # Noise energy
    dSigEng = dEng * dFactor / (1 + dFactor) # Signal energy
    snr = 10 * np.log10(dSigEng / dNoiseEng)

    return snr

def analyze_wada_snr(data_partition):
    results = []
    for item in data_partition:
        audio_data = item['audio']['array']
        preprocess_audio_data = preprocess_audio(audio_data)
        snr = wada_snr(preprocess_audio_data)
        if snr_threshold > snr or np.isnan(snr):
            continue
        results.append((item, snr))
    return results

def analyze_mos(data_partition, gpu_id):
    results = []
    torch.cuda.set_device(gpu_id)
    with torch.no_grad():
        device = torch.device(f"cuda:{gpu_id}")
        predictor = torch.hub.load("tarepan/SpeechMOS:v1.2.0", "utmos22_strong", trust_repo=True)
        predictor = predictor.to(device)
        for item, snr in data_partition:
            try:
                if not np.isnan(snr):
                    sr = item['audio'].get('sampling_rate', 16000)
                    preprocess_audio_data = preprocess_audio(item['audio']['array'])

                    audio_data_tensor = torch.from_numpy(preprocess_audio_data).unsqueeze(0).to(torch.float32).to(device)
                    score = predictor(audio_data_tensor.to(torch.float32), sr).cpu().item()
                    if score >= score_threshold:
                        results.append((item, snr, score))
            except Exception as e:
                logger.exception(e)
    return results

def analyze_whisper(data_partition, gpu_id):
    results = []
    torch.cuda.set_device(gpu_id)
    if not args.skip_whisper:
        whisper_model = whisper.load_model("large").to(torch.device(f"cuda:{gpu_id}"))
    for item, snr, score in data_partition:
        if not args.skip_whisper:
            audio_data = item['audio']['array'].astype(np.float32)
            transcription = whisper_model.transcribe(audio_data, language='ja')['text']
            result = {
                'id': item['id'],
                'uuid': item['utt_id'],
                'snr': float(snr),
                'score': float(score),
                'transcription': transcription,
                'source_transcription': item['text'],
                'path': item['audio']['path']
            }
        else:
            result = {
                'id': item['id'],
                'uuid': item['utt_id'],
                'snr': float(snr),
                'score': float(score),
                'transcription': item['text'],
                'source_transcription': item['text'],
                'path': item['audio']['path']
            }
        results.append(result)
    return results

def process_results(ds, snr_threshold, score_threshold, start, end, data_dir):
    num_cpus = mp.cpu_count()

    # wada_snrの並列処理（CPUの数に基づいて）
    start_time_wada_snr = time.time()
    with ProcessPoolExecutor(max_workers=num_cpus) as executor:
        data_partitions = np.array_split(ds, num_cpus)
        wada_snr_results = list(executor.map(analyze_wada_snr, data_partitions))
    wada_snr_results = [item for sublist in wada_snr_results for item in sublist]
    end_time_wada_snr = time.time()
    elapsed_time_wada_snr = end_time_wada_snr - start_time_wada_snr
    logger.info(f"analyze_wada_snr processing time: {elapsed_time_wada_snr:.4f} seconds")

    # mosの並列処理
    start_time_mos = time.time()
    with ProcessPoolExecutor(max_workers=num_gpus) as executor:
        mos_args = [(data_partition, gpu_id) for gpu_id, data_partition in enumerate(np.array_split(wada_snr_results, num_gpus))]
        mos_results = list(executor.map(analyze_mos, *zip(*mos_args)))
    mos_results = [item for sublist in mos_results for item in sublist]
    end_time_mos = time.time()
    elapsed_time_mos = end_time_mos - start_time_mos
    logger.info(f"analyze_mos processing time: {elapsed_time_mos:.4f} seconds")

    # whisperの並列処理
    start_time_whisper = time.time()
    with ProcessPoolExecutor(max_workers=num_gpus) as executor:
        whisper_args = [(data_partition, gpu_id) for gpu_id, data_partition in enumerate(np.array_split(mos_results, num_gpus))]
        all_results = list(executor.map(analyze_whisper, *zip(*whisper_args)))
    all_results = [item for sublist in all_results for item in sublist]
    end_time_whisper = time.time()
    elapsed_time_whisper = end_time_whisper - start_time_whisper
    logger.info(f"analyze_whisper processing time: {elapsed_time_whisper:.4f} seconds")

    logger.info(f"all_results {len(all_results)}")

    # wavディレクトリを作成
    wav_dir = os.path.join(data_dir, 'raw')
    os.makedirs(wav_dir, exist_ok=True)

    with open(os.path.join(data_dir, f'esd_{start}-{end}.list'), 'w', encoding='utf-8') as f:
        for result in all_results:
            uuid = result["uuid"]
            f.write(f"{uuid}.wav|yodasja|JP|{result['transcription']}\n")
            src_path = result['path']
            dst_path = os.path.join(wav_dir, f"{uuid}.wav")
            shutil.copy(src_path, dst_path)

    with open(os.path.join(data_dir, f'results_{start}-{end}.json'), 'w', encoding='utf-8') as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)

if __name__ == '__main__':

    logger.info(f"{args.start} - {args.end}")

    logger.info(f"data_dir {args.data_dir}")
    # データセットの読み込み
    if args.end is None:
        ds0 = load_dataset('espnet/yodas', dataset_name, split="train", trust_remote_code=True)
    else:
        ds0 = load_dataset('espnet/yodas', dataset_name, split=f'train[{args.start}:{args.end}]' , trust_remote_code=True)
    logger.info(f"データ数: {len(ds0)}")

    # バッチ処理
    batch_size = args.batch_size
    for batch_start in range(args.start, len(ds0), batch_size):
        batch_end = min(batch_start + batch_size - 1, len(ds0))
        logger.info(f"start batch {batch_start} - {batch_end}")
        batch_ds = load_dataset('espnet/yodas', dataset_name, split=f'train[{batch_start}:{batch_end}]' , trust_remote_code=True)

        logger.info(f"Processing batch: {batch_start} - {batch_end}")

        # 分析を実行
        process_results(batch_ds, snr_threshold=args.snr_threshold, score_threshold=args.score_threshold, start=batch_start, end=batch_end, data_dir=args.data_dir)

     # 全てのesd.listをマージ
    esd_files = glob.glob(os.path.join(args.data_dir, 'esd_*.list'))
    with open(os.path.join(args.data_dir, 'esd.list'), 'w', encoding='utf-8') as outfile:
        for esd_file in esd_files:
            with open(esd_file, 'r', encoding='utf-8') as infile:
                outfile.write(infile.read())

    logger.info(f"All esd.list files merged into {os.path.join(args.data_dir, 'esd.list')}")